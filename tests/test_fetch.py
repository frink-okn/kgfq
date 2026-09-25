import math

import pytest

from kgfq.client.endpoint import Endpoint, Pattern
from kgfq.client.terms import Iri, Var
from kgfq.plan import execute_fetch, plan_fetch
from kgfq.workspace.receipts import fetch_receipt
from kgfq.workspace.store import Workspace

EX = "http://example.org/"
AFFECTS = Iri(EX + "affects")


@pytest.fixture(scope="module")
def ep(chem):
    e = Endpoint(chem.origin)
    yield e
    e.close()


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
    assert (
        r["completeness"] == {"complete": True, "truncation_reasons": ["page_limit"]}
        or r["completeness"]["complete"]
    )
