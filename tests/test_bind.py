"""``bind`` (``docs/session/operations.yaml``): a pattern's variables take the
values of an earlier table's columns. Against two live fixture servers, since
its point is the join across them."""

import io
import json
from pathlib import Path

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from kgfq.cli import main
from kgfq.client.terms import BadTermSyntax, BNode, Iri, Literal
from kgfq.workspace.bind import bind_rows, parse_bind
from kgfq.workspace.receipts import new_receipt
from kgfq.workspace.store import Workspace, WorkspaceError

EX = "http://example.org/"
XSD = "http://www.w3.org/2001/XMLSchema#"
REGISTRY = Path(__file__).parent.parent / "docs" / "session" / "operations.yaml"
OPERATIONS = yaml.safe_load(REGISTRY.read_text())["operations"]


def run(*argv, workspace, expect=0):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--workspace", str(workspace), "--json", *argv], out=out, err=err)
    assert code == expect, f"exit {code}: stderr={err.getvalue()!r} stdout={out.getvalue()[:300]!r}"
    text = out.getvalue()
    return (json.loads(text) if text.lstrip().startswith(("{", "[")) else text), err.getvalue()


@pytest.fixture
def ws(tmp_path, chem, genes):
    run("endpoints", "add", chem.origin, workspace=tmp_path)
    run("endpoints", "add", genes.origin, workspace=tmp_path)
    return tmp_path


def receipt(shape="bindings"):
    return new_receipt(
        kind="fetch",
        step=1,
        grade="bundle",
        sources=[],
        shape=shape,
        completeness={"complete": True, "incomplete_because": []},
        totals={"requests": 0},
    )


def strings(columns: dict) -> pa.Table:
    return pa.table({c: pa.array(v, type=pa.string()) for c, v in columns.items()})


def test_parse_bind():
    assert parse_bind("chems:c=chem") == {"table": "chems", "columns": {"c": "chem"}}
    assert parse_bind("t:?c=chem, g=gene") == {"table": "t", "columns": {"c": "chem", "g": "gene"}}
    assert parse_bind("chems:chem") == {"table": "chems", "columns": {"chem": "chem"}}
    for bad in ("chems", "chems:", ":c=chem", "chems:c=", "chems:=chem", "chems:9c=chem"):
        with pytest.raises(BadTermSyntax):
            parse_bind(bad)


def test_bound_rows_are_the_tables_distinct_terms_read_back_whole(tmp_path):
    """Kind, language and datatype come back from the columns beside the value,
    so what is sent is the term that was fetched: `"1"` the string, `"1"` the
    integer and `<1>` are three values. A row with no value binds nothing."""
    ws = Workspace(tmp_path)
    table = strings(
        {
            "x": ["http://x/a", "1", "1", "chat", "_:b0", "http://x/a", None],
            "x_kind": ["iri", "literal", "literal", "literal", "bnode", "iri", None],
            "x_lang": [None, None, None, "fr", None, None, None],
            "x_datatype": [None, None, XSD + "integer", None, None, None, None],
            "n": ["1", "2", "3", "4", "5", "1", "7"],
            "n_kind": ["literal"] * 7,
            "n_lang": [None] * 7,
            "n_datatype": [None] * 7,
        }
    )
    ws.write_table("t", table, receipt())
    assert bind_rows(ws, {"table": "t", "columns": {"v": "x"}}) == (
        ["v"],
        [
            [Iri("http://x/a")],
            [Literal("1")],
            [Literal("1", datatype=XSD + "integer")],
            [Literal("chat", lang="fr")],
            [BNode("b0")],
        ],
    )
    # two variables are the distinct pairs, in the table's order
    vars_, rows = bind_rows(ws, {"table": "t", "columns": {"v": "x", "k": "n"}})
    assert vars_ == ["v", "k"] and len(rows) == 5
    assert rows[0] == [Iri("http://x/a"), Literal("1")]


def test_what_cannot_be_bound_is_refused_with_what_there_is(tmp_path):
    ws = Workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="no table 't' to bind from; tables so far: none"):
        bind_rows(ws, {"table": "t", "columns": {"v": "x"}})
    ws.write_table("t", strings({"x": ["a"], "x_kind": ["iri"], "bare": ["b"]}), receipt())
    with pytest.raises(
        WorkspaceError, match="no column 'y' to bind \\?v to; its columns are x, bare"
    ):
        bind_rows(ws, {"table": "t", "columns": {"v": "y"}})
    # a companion is not a column of terms
    with pytest.raises(WorkspaceError, match="no column 'x_kind'"):
        bind_rows(ws, {"table": "t", "columns": {"v": "x_kind"}})
    with pytest.raises(WorkspaceError, match="has no `bare_kind` beside it"):
        bind_rows(ws, {"table": "t", "columns": {"v": "bare"}})
    # a triples table's subject and predicate have no companions and need none
    triples = strings(
        {
            "s": ["http://x/a", "_:b1"],
            "p": ["http://x/p"] * 2,
            "o": ["v", "w"],
            "o_kind": ["literal"] * 2,
            "o_lang": [None] * 2,
            "o_datatype": [None] * 2,
        }
    )
    ws.write_table("g", triples, receipt("triples"))
    assert bind_rows(ws, {"table": "g", "columns": {"x": "s"}})[1] == [
        [Iri("http://x/a")],
        [BNode("b1")],
    ]
    assert bind_rows(ws, {"table": "g", "columns": {"x": "p"}})[1] == [[Iri("http://x/p")]]


def test_a_bound_fetch_joins_two_graphs_on_two_servers(ws, tmp_path):
    """The chemicals' genes, fetched from one server, bind the next fetch on the
    other: the genes' pathways. The same answer a values file gives, with the
    table named as the step's input and no file between."""
    run("fetch", "chemkg", "ex:chem/1 ex:affects ?gene", "--out", "affected", workspace=ws)
    genes = sorted(pq.read_table(ws / "tables" / "affected.parquet")["gene"].to_pylist())
    assert genes == [f"{EX}gene/G2", f"{EX}gene/G4"]
    handle, _ = run(
        *("fetch", "genekg", "?gene ex:inPathway ?pathway"),
        *("--bind", "affected:gene", "--out", "pathways"),
        workspace=ws,
    )
    table = pq.read_table(ws / "tables" / "pathways.parquet")
    assert handle["rows"] == 2 and sorted(table["gene"].to_pylist()) == genes
    values = tmp_path / "genes.tsv"
    values.write_text("?gene\n" + "".join(f"<{g}>\n" for g in genes))
    by_file, _ = run(
        *("fetch", "genekg", "?gene ex:inPathway ?pathway"),
        *("--values", str(values), "--out", "by_file"),
        workspace=ws,
    )
    assert by_file["content_hash"] == handle["content_hash"]
    log, _ = run("log", workspace=ws)
    record = log["steps"][1]
    assert record["args"] == {
        "graph": "genekg",
        "subject": "?gene",
        "predicate": f"<{EX}inPathway>",
        "object": "?pathway",
        "bind": {"table": "affected", "columns": {"gene": "gene"}},
        "out": "pathways",
    }
    jsonschema.Draft202012Validator(OPERATIONS["fetch"]["args"]).validate(record["args"])
    derived = {"kind": "derived", "table": "affected", "step": 1, "grade": "bundle"}
    assert record["inputs"]["affected"] == derived
    assert record["receipt"]["core"]["inputs"]["affected"] == derived
    assert record["receipt"]["detail"]["plan"]["steps"][-1]["op"] == "bindings"
    # the two steps read two servers
    assert log["steps"][0]["pins"][0]["origin"] != record["pins"][0]["origin"]


def test_bind_names_another_variable_and_serves_count_distinct_and_explain(ws):
    run(
        "fetch",
        "chemkg",
        "?chem ex:affects ?gene",
        "--max-rows",
        "6",
        "--out",
        "some",
        workspace=ws,
    )
    chems = sorted(set(pq.read_table(ws / "tables" / "some.parquet")["chem"].to_pylist()))
    payload, _ = run("count", "chemkg", "?c ex:affects ?", "--bind", "some:c=chem", workspace=ws)
    assert sorted(c["row"] for c in payload["counts"]) == sorted([f"<{c}>"] for c in chems)
    assert payload["total"] == 2 * len(chems)
    plan, _ = run("explain", "chemkg", "?c ex:affects ?g", "--bind", "some:c=chem", workspace=ws)
    assert plan["intent"]["bindings"] == {"vars": ["c"], "rows": len(chems), "distinct": len(chems)}
    answer, _ = run(
        "distinct", "chemkg", "?c ? ?", "--by", "p", "--bind", "some:c=chem", workspace=ws
    )
    affects = next(r for r in answer["rows"] if r["value"] == f"<{EX}affects>")
    assert affects["bindings"] == len(chems)
    log, _ = run("log", workspace=ws)
    for record in log["steps"][1:]:
        assert record["args"]["bind"] == {"table": "some", "columns": {"c": "chem"}}
        assert record["inputs"]["some"]["step"] == 1
        schema = OPERATIONS[record["operation"]]["args"]
        jsonschema.Draft202012Validator(schema).validate(record["args"])


def test_bind_refusals_reach_the_caller_before_any_request(ws, tmp_path):
    run("fetch", "chemkg", "?chem ex:affects ?gene", "--out", "affects", workspace=ws)
    values = tmp_path / "v.tsv"
    values.write_text("?chem\n<http://example.org/chem/1>\n")
    for argv, said in (
        (["--bind", "affects:chem", "--values", str(values)], "pass one"),
        (["--bind", "nowhere:chem"], "no table 'nowhere' to bind from; tables so far: affects"),
        (["--bind", "affects:chem=chemical"], "its columns are chem, gene"),
        (["--bind", "affects"], "--bind is `table:variable=column"),
        # the pattern names ?chem and ?gene, not ?c (C1)
        (["--bind", "affects:c=chem"], "binding variable ?c does not appear in the pattern"),
    ):
        for command in ("count", "explain", "fetch"):
            _, err = run(command, "chemkg", "?chem ex:affects ?gene", *argv, workspace=ws, expect=2)
            assert said in err, err
    log, _ = run("log", workspace=ws)
    assert len(log["steps"]) == 1


def test_a_bound_step_says_which_values_found_nothing(ws, tmp_path):
    """genekg holds seven genes and chemkg affects five of them. Binding all
    seven finds rows for five, and the receipt, the record and the handle say
    so, with the two that found nothing in request syntax. A table of the
    matches alone would not show the loss."""
    run("fetch", "genekg", "?gene rdf:type ex:Gene", "--out", "genes", workspace=ws)
    handle, _ = run(
        *("fetch", "chemkg", "?chem ex:affects ?gene"),
        *("--bind", "genes:gene", "--out", "affected"),
        workspace=ws,
    )
    lost = [[f"<{EX}gene/G6>"], [f"<{EX}gene/G7>"]]
    report = {
        "vars": ["gene"],
        "sent": 7,
        "table": "genes",
        "columns": {"gene": "gene"},
        "matched": 5,
        "unmatched": 2,
        "unmatched_sample": lost,
    }
    assert handle["bound"] == report and handle["complete"] is True
    receipt, _ = run("receipt", "affected", workspace=ws)
    assert receipt["core"]["bindings"] == report
    counted, _ = run("count", "chemkg", "? ex:affects ?gene", "--bind", "genes:gene", workspace=ws)
    assert counted["bound"] == report and counted["total"] == handle["rows"]
    log, _ = run("log", workspace=ws)
    assert log["steps"][1]["receipt"]["core"]["bindings"] == report
    assert log["steps"][2]["receipt"]["core"]["bindings"] == report
    assert log["steps"][2]["result"] == {"total": handle["rows"], "bound": report}

    # a file gives the same report, naming the file; a tuple given twice is one
    values = tmp_path / "chems.tsv"
    values.write_text("?chem\n" + "".join(f"<{EX}chem/{i}>\n" for i in (1, 99, 1)))
    handle, _ = run(
        "fetch", "chemkg", "?chem ex:affects ?", "--values", str(values), "--out", "f", workspace=ws
    )
    assert handle["bound"] == {
        "vars": ["chem"],
        "sent": 2,
        "values": str(values),
        "matched": 1,
        "unmatched": 1,
        "unmatched_sample": [[f"<{EX}chem/99>"]],
    }

    # a read that stopped early cannot say what the rest would have matched
    handle, _ = run(
        *("fetch", "chemkg", "?chem ex:affects ?gene", "--bind", "genes:gene"),
        *("--max-rows", "3", "--out", "few"),
        workspace=ws,
    )
    assert handle["complete"] is False
    assert handle["bound"]["sent"] == 7 and 1 <= handle["bound"]["matched"] <= 3
    assert handle["bound"]["unmatched"] is None and handle["bound"]["unmatched_sample"] == []
