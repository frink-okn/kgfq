"""The fetch guard (plan C4): a fetch that would read more rows than the guard
allows is refused before a row is read, unless the caller bounds it or asks for
all of it. Against a live fixture server, with the guard set low: the fixture's
largest predicate is 24 triples and the tests' guard is 15."""

import io
import json

import pytest

from kgfq.cli import main
from kgfq.client.endpoint import Endpoint, Pattern
from kgfq.client.errors import TooLarge
from kgfq.client.terms import Iri, Var
from kgfq.plan import check_guard, guard_rows, plan_fetch
from kgfq.plan.fetch import Plan, PlanStep

EX = "http://example.org/"
AFFECTS = Iri(EX + "affects")


def run(*argv, workspace, expect=0):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--workspace", str(workspace), "--json", *argv], out=out, err=err)
    assert code == expect, f"exit {code}: stderr={err.getvalue()!r} stdout={out.getvalue()[:300]!r}"
    text = out.getvalue()
    return (json.loads(text) if text.lstrip().startswith(("{", "[")) else text), err.getvalue()


@pytest.fixture
def ws(tmp_path, chem, monkeypatch):
    monkeypatch.setenv("KGFQ_FETCH_GUARD_ROWS", "15")
    run("endpoints", "add", chem.origin, workspace=tmp_path)
    return tmp_path


def test_the_guards_limit(monkeypatch):
    monkeypatch.delenv("KGFQ_FETCH_GUARD_ROWS", raising=False)
    assert guard_rows() == 1_000_000
    for text, limit in (("250000", 250_000), ("2_000", 2000), ("off", None), ("0", None)):
        monkeypatch.setenv("KGFQ_FETCH_GUARD_ROWS", text)
        assert guard_rows() == limit
    monkeypatch.setenv("KGFQ_FETCH_GUARD_ROWS", "lots")
    with pytest.raises(TooLarge, match="is not a number of rows"):
        guard_rows()


def test_a_fetch_over_the_guard_is_refused_before_a_row_is_read(ws):
    """24 triples against a guard of 15. The refusal says what it would have
    cost and what would make it smaller; nothing is logged or written."""
    _, err = run("fetch", "chemkg", "? ex:affects ?", workspace=ws, expect=2)
    assert err.startswith("kgfq: `? <http://example.org/affects> ?` in chemkg matches 24 triples")
    assert "24 triples: 1 request, and" in err
    assert "over the guard of 15 rows, so nothing was fetched" in err
    assert "`--bind`" in err and "`--max-rows N`" in err and "`kgfq distinct`" in err
    assert err.rstrip().endswith("To fetch it all, pass `--all`.")
    # the pattern typed by accident costs a count
    _, err = run("fetch", "chemkg", "? ? ?", workspace=ws, expect=2)
    assert "`? ? ?` in chemkg matches 61 triples" in err
    log, _ = run("log", workspace=ws)
    assert log["steps"] == [] and not (ws / "tables").exists()

    # explain shows the same verdict and refuses nothing
    plan, _ = run("explain", "chemkg", "? ex:affects ?", workspace=ws)
    assert plan["evidence"]["guard"] == {"rows": 15, "reads": 24, "over": True}

    # a part of it, or all of it on purpose
    handle, _ = run("fetch", "chemkg", "? ex:affects ?", "--max-rows", "5", workspace=ws)
    assert handle["rows"] == 5
    handle, _ = run("fetch", "chemkg", "? ex:affects ?", "--all", "--out", "whole", workspace=ws)
    assert handle["rows"] == 24 and handle["complete"] is True
    log, _ = run("log", workspace=ws)
    assert log["steps"][-1]["args"]["all"] is True and "all" not in log["steps"][0]["args"]

    # under the guard nothing changes
    handle, _ = run("fetch", "chemkg", "? rdfs:label ?", "--out", "labels", workspace=ws)
    assert handle["rows"] == 12
    receipt, _ = run("receipt", "labels", workspace=ws)
    assert receipt["detail"]["plan"]["evidence"]["guard"] == {
        "rows": 15,
        "reads": 12,
        "over": False,
    }


def test_a_bound_fetch_is_sized_by_what_its_values_reach(ws, tmp_path):
    """The pattern is over the guard, so the bind-join is not waved through on
    the pattern's count: one bound count sums what the values reach. Two
    chemicals reach 4 triples and are fetched; twelve reach all 24 and are not."""
    few = tmp_path / "few.tsv"
    few.write_text("?c\n" + "".join(f"<{EX}chem/{i}>\n" for i in (1, 2)))
    handle, _ = run(
        "fetch", "chemkg", "?c ex:affects ?g", "--values", str(few), "--out", "few", workspace=ws
    )
    assert handle["rows"] == 4
    receipt, _ = run("receipt", "few", workspace=ws)
    evidence = receipt["detail"]["plan"]["evidence"]
    assert evidence["bound_count"] == {"value": 4, "exact": True}
    assert evidence["guard"] == {"rows": 15, "reads": 4, "over": False}

    every = tmp_path / "every.tsv"
    every.write_text("?c\n" + "".join(f"<{EX}chem/{i}>\n" for i in range(1, 13)))
    _, err = run(
        "fetch", "chemkg", "?c ex:affects ?g", "--values", str(every), workspace=ws, expect=2
    )
    assert "the 12 bound values reach 24 triples of `?c <http://example.org/affects> ?g`" in err
    assert "Bind fewer values, or take a part (`--max-rows N`)." in err
    handle, _ = run(
        *("fetch", "chemkg", "?c ex:affects ?g", "--values", str(every)),
        *("--max-rows", "6", "--out", "part"),
        workspace=ws,
    )
    assert handle["rows"] == 6

    # a pattern under the guard needs no second look: no bound count is made
    handle, _ = run(
        "fetch", "chemkg", "?c rdfs:label ?l", "--values", str(every), "--out", "l", workspace=ws
    )
    receipt, _ = run("receipt", "l", workspace=ws)
    assert "bound_count" not in receipt["detail"]["plan"]["evidence"]
    assert receipt["detail"]["plan"]["evidence"]["guard"]["over"] is False


def test_a_scan_that_filters_is_not_bounded_by_max_rows(chem):
    """Where R2 picks the scan under bindings, the read is the whole pattern
    whatever is kept, so `max_rows` does not make it small; only asking for all
    of it does."""
    ep = Endpoint(chem.origin)
    try:
        rel = ep.release("chemkg")
        pattern = Pattern(s=Var("c"), p=AFFECTS, o=Var("g"))
        rows = [[Iri(f"{EX}chem/{i}")] for i in (1, 2, 3)]
        ep.service_descriptor()["caps"]["max_bindings"] = 1
        plan = plan_fetch(ep, rel, pattern, vars_=["c"], rows=rows, guard=10)
        assert plan.steps[-1].op == "fragment" and plan.evidence["guard"]["over"] is True
        with pytest.raises(TooLarge, match="filtering for 3 bound values would read all 24"):
            check_guard(plan, pattern, rel, max_rows=5)
        check_guard(plan, pattern, rel, allow_all=True)
        # no guard asked for, no verdict, nothing refused
        unguarded = plan_fetch(ep, rel, pattern, vars_=["c"], rows=rows)
        assert "guard" not in unguarded.evidence
        check_guard(unguarded, pattern, rel)
    finally:
        ep.close()


def test_the_refusal_says_what_was_not_spent():
    """41 M triples, the largest predicate the guard has been wanted for: 4,100
    pages and eight to fifteen minutes."""
    plan = Plan(
        intent={},
        steps=[PlanStep("fragment", {}, 4_100, 41_000_000)],
        evidence={"guard": {"rows": 1_000_000, "reads": 41_000_000, "over": True}},
    )

    class Rel:
        dataset = "ufokn"

    with pytest.raises(TooLarge) as e:
        check_guard(plan, Pattern(p=Iri("https://schema.org/value")), Rel())  # ty: ignore[invalid-argument-type] — a stand-in for a Release; only its name is read
    text = str(e.value)
    assert "matches 41,000,000 triples: 4,100 requests, and about 8 min to 15 min" in text
    assert "over the guard of 1,000,000 rows" in text
