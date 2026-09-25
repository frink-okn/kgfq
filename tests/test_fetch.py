import math
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from kgfq.client.endpoint import Endpoint, Pattern, RequestRecord
from kgfq.client.statistics import Statistics
from kgfq.client.terms import Iri, Var
from kgfq.plan import execute_fetch, plan_fetch
from kgfq.workspace.receipts import completeness, fetch_receipt
from kgfq.workspace.store import Workspace

EX = "http://example.org/"
AFFECTS = Iri(EX + "affects")
COMPLETE = {"complete": True, "incomplete_because": [], "truncation_reasons": []}
PAGED_TO_ITS_END = {
    "complete": True,
    "incomplete_because": [],
    "truncation_reasons": ["page_limit"],
}


@pytest.fixture(scope="module")
def ep(chem):
    e = Endpoint(chem.origin)
    yield e
    e.close()


@contextmanager
def caps(e: Endpoint, **values: int) -> Iterator[None]:
    """Smaller caps than the server publishes. The server honours any limit
    under its own ``max_limit`` (10,000 here), so the paging is the real server's."""
    e.service_descriptor()["caps"].update(values)
    try:
        yield
    finally:
        e._service = None


def fetched(e, rel, pattern, *, vars_=None, rows=None, max_rows=None, stats=None):
    with e.trace() as t:
        plan = plan_fetch(e, rel, pattern, vars_=vars_, rows=rows, stats=stats)
        table = execute_fetch(e, rel, pattern, plan, vars_=vars_, rows=rows, max_rows=max_rows)
        receipt = fetch_receipt(
            step=1, kind="fetch", release=rel, intent=plan.intent, plan=plan.as_dict(), trace=t
        )
    return plan, table, receipt


def pages(receipt):
    """The responses that carried the table's rows."""
    return [q for q in receipt["requests"] if q["rows"] is not None]


def test_plan_counts_first_and_pages_at_max_limit(ep):
    rel = ep.release("chemkg")
    with ep.trace() as t:
        plan = plan_fetch(ep, rel, Pattern(p=AFFECTS))
    assert t.requests == 1 and plan.evidence["count"] == {
        "value": 24,
        "exact": True,
        "source": "server",
    }
    assert [s.op for s in plan.steps] == ["fragment"]
    assert plan.estimated_requests == math.ceil(24 / ep.cap("max_limit", 10000)) == 1
    d = plan.as_dict()
    assert d["schema"] == "kgfq.plan/1" and d["steps"][0]["note"].startswith("R1")


def test_r2_chooses_bind_join_or_column_fetch_from_the_caps(ep):
    rel = ep.release("chemkg")
    subjects = [[Iri(f"{EX}chem/{i}")] for i in (1, 2, 3)]
    pattern = Pattern(s=Var("c"), p=AFFECTS, o=Var("g"))
    plan = plan_fetch(ep, rel, pattern, vars_=["c"], rows=subjects)
    assert plan.steps[-1].op == "bindings" and plan.evidence["r2"]["k"] == 3
    bound = execute_fetch(ep, rel, pattern, plan, vars_=["c"], rows=subjects)

    ep.service_descriptor()["caps"]["max_bindings"] = 1
    try:
        plan2 = plan_fetch(ep, rel, pattern, vars_=["c"], rows=subjects)
    finally:
        ep._service = None
    assert plan2.steps[-1].op == "fragment" and "column-fetch" in plan2.steps[-1].note
    scanned = execute_fetch(ep, rel, pattern, plan2, vars_=["c"], rows=subjects)

    def as_set(t):
        return set(
            zip(t["s"].to_pylist(), t["o"].to_pylist(), t["binding"].to_pylist(), strict=True)
        )

    assert as_set(bound) == as_set(scanned) and len(as_set(bound)) == 6
    assert set(bound["p"].to_pylist()) == {EX + "affects"}


def test_fetch_to_workspace_with_receipt(ep, tmp_path):
    rel = ep.release("chemkg")
    pattern = Pattern(p=Iri("http://www.w3.org/2000/01/rdf-schema#label"))
    ws = Workspace(tmp_path)
    with ep.trace() as t:
        plan = plan_fetch(ep, rel, pattern)
        table = execute_fetch(ep, rel, pattern, plan, max_rows=None)
        receipt = fetch_receipt(
            step=1, kind="fetch", release=rel, intent=plan.intent, plan=plan.as_dict(), trace=t
        )
    info = ws.write_table("labels", table, receipt)
    assert info.rows == 12 and info.columns == ["s", "p", "o", "o_kind", "o_lang", "o_datatype"]
    assert set(table["o_kind"].to_pylist()) == {"literal"}
    assert set(table["o_lang"].to_pylist()) == {"en"}
    r = ws.read_receipt("labels")
    assert r["grade"] == "bundle" and r["source"]["content_digest"] == rel.content_digest
    assert r["totals"]["requests"] == t.requests == 2 and all(
        q["request_id"] for q in r["requests"]
    )
    assert r["completeness"] == COMPLETE


# -- completeness (C6): what the read left behind, not the server's per-page flag


def test_a_fetch_paged_to_its_end_is_complete(ep):
    """Every page but the last says ``complete: false``/``page_limit`` and each
    cursor was followed, so every row was fetched. The trap, found 2026-09-24:
    kwg-places, 33,791 rows over four pages, receipted incomplete."""
    rel = ep.release("chemkg")
    with caps(ep, max_limit=5):
        plan, table, receipt = fetched(ep, rel, Pattern(p=AFFECTS))
    assert plan.estimated_requests == 5
    assert table.num_rows == 24 == plan.evidence["count"]["value"]
    assert [
        (q["rows"], q["complete"], q["truncation_reason"], q["continued"]) for q in pages(receipt)
    ] == [(5, False, "page_limit", True)] * 4 + [(4, True, None, False)]
    assert not any(q["stopped"] for q in receipt["requests"])
    assert receipt["completeness"] == PAGED_TO_ITS_END


@pytest.mark.parametrize(
    ("max_limit", "max_rows", "stopped_at"),
    [
        (5, 3, 0),  # mid the first page
        (5, 10, 1),  # the end of a page whose cursor was not followed
        (5, 22, 4),  # mid the last page, which the server called complete
        (10000, 3, 0),  # one page, complete by the server's flag, cut short
        (5, 24, None),  # the last row of the last page: nothing left behind
        (5, 30, None),
    ],
)
def test_a_fetch_stopped_at_max_rows_is_incomplete_only_when_rows_remained(
    ep, max_limit, max_rows, stopped_at
):
    rel = ep.release("chemkg")
    with caps(ep, max_limit=max_limit):
        _, table, receipt = fetched(ep, rel, Pattern(p=AFFECTS), max_rows=max_rows)
    assert table.num_rows == min(max_rows, 24)
    marked = [i for i, q in enumerate(pages(receipt)) if q["stopped"]]
    if stopped_at is None:
        assert marked == [] and receipt["completeness"]["complete"] is True
    else:
        assert marked == [stopped_at] and pages(receipt)[stopped_at]["stopped"] == "max_rows"
        assert receipt["completeness"]["complete"] is False
        assert receipt["completeness"]["incomplete_because"] == ["max_rows"]


FIVE = [[Iri(f"{EX}chem/{i}")] for i in (1, 2, 3, 4, 5)]
BOUND = Pattern(s=Var("c"), p=AFFECTS, o=Var("g"))


def test_a_bindings_fetch_across_chunks_and_pages_is_complete(ep):
    """Three chunks of at most two chemicals, pages of three rows: the first two
    chunks each span two pages, and every chunk-relative cursor is followed."""
    rel = ep.release("chemkg")
    with caps(ep, max_limit=3, max_bindings=2):
        plan, table, receipt = fetched(ep, rel, BOUND, vars_=["c"], rows=FIVE)
    assert plan.steps[-1].op == "bindings" and table.num_rows == 10
    assert [(q["bindings"], q["rows"], q["continued"]) for q in pages(receipt)] == [
        (2, 3, True),
        (2, 1, False),
        (2, 3, True),
        (2, 1, False),
        (1, 2, False),
    ]
    assert receipt["completeness"] == PAGED_TO_ITS_END


@pytest.mark.parametrize(
    ("max_rows", "requests", "complete"),
    [
        (2, 1, False),  # mid a chunk's first page
        (4, 2, False),  # a chunk's last row, with two chunks not sent
        (8, 4, False),  # the second chunk's last row: the third not sent
        (10, 5, True),  # the final chunk's last row
    ],
)
def test_a_bindings_fetch_stopped_early_is_incomplete_only_when_rows_remained(
    ep, max_rows, requests, complete
):
    rel = ep.release("chemkg")
    with caps(ep, max_limit=3, max_bindings=2):
        _, table, receipt = fetched(ep, rel, BOUND, vars_=["c"], rows=FIVE, max_rows=max_rows)
    assert table.num_rows == max_rows and len(pages(receipt)) == requests
    assert receipt["completeness"]["complete"] is complete
    assert receipt["completeness"]["incomplete_because"] == ([] if complete else ["max_rows"])
    assert [q["stopped"] for q in pages(receipt)][-1] == (None if complete else "max_rows")


def test_the_plans_evidence_does_not_decide_the_tables_completeness(ep, tmp_path):
    """The statistics page ``/schema`` in the fetch's trace, each page but the
    last ``complete: false``; none carried a row of the table."""
    rel = ep.release("chemkg")
    stats = Statistics(Workspace(tmp_path), ep, rel)
    with caps(ep, max_schema_items=1):
        plan, table, receipt = fetched(ep, rel, Pattern(p=AFFECTS), stats=stats)
    schema = [q for q in receipt["requests"] if q["url"].split("?")[0].endswith("/schema")]
    limited = [q for q in schema if q["complete"] is False]
    assert limited and all(q["truncation_reason"] == "page_limit" for q in limited)
    assert all(q["continued"] and q["rows"] is None for q in limited)
    assert plan.evidence["count"]["source"] == "inventory:predicate" and table.num_rows == 24
    assert receipt["completeness"] == COMPLETE


def _page(complete: bool, reason: str | None, *, continued: bool = False) -> RequestRecord:
    return RequestRecord(
        method="GET",
        url="/fragment",
        status=200,
        request_id=None,
        elapsed_ms=0.0,
        bytes_wire=0,
        bytes_decoded=0,
        complete=complete,
        truncation_reason=reason,
        rows=5,
        continued=continued,
    )


@pytest.mark.parametrize(
    ("reason", "continued", "complete"),
    [
        ("page_limit", True, True),
        ("time_budget", True, True),
        ("response_bytes", True, True),
        ("candidate_budget", True, False),
        ("time_budget", False, False),
        ("response_bytes", False, False),  # kgf-rs stops some byte budgets without a cursor
    ],
)
def test_which_continued_truncations_leave_a_table_complete(reason, continued, complete):
    """The rule over records, since a fixture server cannot be given a budget:
    ``kgf serve`` has no flag for one, and the fixtures fit its 64 MB and 2 s.
    That following a ``response_bytes`` cursor loses and repeats nothing is
    kgf-rs's own test (``a_response_stops_at_the_byte_budget_and_resumes…``)."""
    records = [_page(False, reason, continued=continued)]
    if continued:
        records.append(_page(True, None))
    got = completeness(records)
    assert got["complete"] is complete and got["truncation_reasons"] == [reason]
    assert got["incomplete_because"] == ([] if complete else [reason])
