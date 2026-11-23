"""The one-pattern templates (``kgfq.ops.template``): ``describe``,
``incoming``, ``instances`` and ``neighbors``, each a fetch the plan sizes and
the guard bounds. Against two live fixture servers and the files they were
built from."""

import io
import json
from pathlib import Path

import jsonschema
import pyarrow.parquet as pq
import pytest
import rdflib
import yaml

from kgfq.cli import main

EX = "http://example.org/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
FIXTURES = Path(__file__).parent / "fixtures"
SESSION = Path(__file__).parent.parent / "docs" / "session"
OPERATIONS = yaml.safe_load((SESSION / "operations.yaml").read_text())["operations"]
RECEIPT = jsonschema.Draft202012Validator(json.loads((SESSION / "receipt.schema.json").read_text()))
ENVELOPE = jsonschema.Draft202012Validator(
    json.loads((SESSION / "envelope.schema.json").read_text())
)


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


@pytest.fixture(scope="module")
def graphs() -> dict[str, rdflib.Graph]:
    out = {}
    for name in ("chem", "genes"):
        out[name] = rdflib.Graph()
        out[name].parse(FIXTURES / f"{name}.nt", format="nt")
    return out


def exported(ws, table) -> set:
    text, _ = run("export", table, workspace=ws)
    g = rdflib.Graph()
    g.parse(data=text, format="nt")
    return set(g)


def test_describe_is_the_triples_an_entity_is_subject_of(ws, graphs):
    handle, _ = run("describe", "chemkg", "ex:chem/1", workspace=ws)
    chem1 = rdflib.URIRef(EX + "chem/1")
    expected = set(graphs["chem"].triples((chem1, None, None)))
    assert handle["table"] == "describe_1" and handle["rows"] == len(expected) == 6
    assert handle["columns"] == ["p", "o"] and handle["complete"] is True
    assert handle["limit_reached"] is False and handle["graphs"] == {"chemkg": 6}
    # the constant subject has no column; the receipt's pattern puts it back
    assert exported(ws, "describe_1") == expected
    # the first rows come back with the handle: an IRI bare, a literal by its tags,
    # a number as a number
    seen = {(r["p"], r["o"]) for r in handle["preview"]}
    assert (RDF_TYPE, EX + "Chemical") in seen
    assert ("http://www.w3.org/2000/01/rdf-schema#label", '"atrazine"@en') in seen
    assert (EX + "mass", 107.5) in seen, "a number is shown as one"
    assert (EX + "synonym", "2-chloro-4-ethylamino-6-isopropylamino-s-triazine") in seen

    receipt, _ = run("receipt", "describe_1", workspace=ws)
    RECEIPT.validate(receipt)
    core = receipt["core"]
    assert (core["kind"], core["shape"]) == ("describe", "bindings")
    assert core["pattern"] == {"s": f"<{EX}chem/1>", "p": "?p", "o": "?o"}
    assert receipt["detail"]["rows_by_graph"] == {"chemkg": 6}
    assert receipt["detail"]["plans"][0]["steps"][-1]["op"] == "fragment"
    log, _ = run("log", workspace=ws)
    [record] = log["steps"]
    ENVELOPE.validate(record)
    assert record["operation"] == "describe" and record["args"] == {
        "graphs": ["chemkg"],
        "iri": EX + "chem/1",
        "limit": 200,
        "out": "describe_1",
    }
    jsonschema.Draft202012Validator(OPERATIONS["describe"]["args"]).validate(record["args"])
    assert record["receipt"] == receipt
    assert record["inputs"][EX + "chem/1"] == {"kind": "asserted"}


def test_incoming_instances_and_neighbors_are_their_patterns(ws, graphs):
    g2 = rdflib.URIRef(EX + "gene/G2")
    affects = rdflib.URIRef(EX + "affects")
    chem1 = rdflib.URIRef(EX + "chem/1")
    handle, _ = run("incoming", "chemkg", f"<{EX}gene/G2>", workspace=ws)
    assert handle["columns"] == ["s", "p"]
    assert exported(ws, handle["table"]) == set(graphs["chem"].triples((None, None, g2)))

    handle, _ = run("instances", "chemkg", "ex:Chemical", "--out", "chemicals", workspace=ws)
    assert handle["columns"] == ["s"] and handle["rows"] == 12
    assert exported(ws, "chemicals") == set(
        graphs["chem"].triples((None, rdflib.RDF.type, rdflib.URIRef(EX + "Chemical")))
    )

    out, _ = run("neighbors", "chemkg", EX + "chem/1", "ex:affects", "--out", "out", workspace=ws)
    assert out["columns"] == ["node"]
    assert exported(ws, "out") == set(graphs["chem"].triples((chem1, affects, None)))
    back, _ = run(
        *("neighbors", "chemkg", "ex:gene/G2", "ex:affects"),
        *("--direction", "in", "--out", "back"),
        workspace=ws,
    )
    assert exported(ws, "back") == set(graphs["chem"].triples((None, affects, g2)))
    assert {r["node"] for r in back["preview"]} == {str(s) for s, _, _ in exported(ws, "back")}
    log, _ = run("log", workspace=ws)
    assert [r["operation"] for r in log["steps"]] == [
        *("incoming", "instances", "neighbors", "neighbors"),
    ]
    assert log["steps"][1]["args"] == {
        "graphs": ["chemkg"],
        "class": EX + "Chemical",
        "limit": 200,
        "out": "chemicals",
    }
    assert (
        log["steps"][3]["args"]["direction"] == "in" and "direction" not in log["steps"][2]["args"]
    )
    for record in log["steps"]:
        schema = OPERATIONS[record["operation"]]["args"]
        jsonschema.Draft202012Validator(schema).validate(record["args"])
        assert set(record["args"]) <= set(schema["properties"])
        assert OPERATIONS[record["operation"]]["implemented_by"]["kgfq"] == record["operation"]


def test_a_template_asks_each_graph_and_says_what_each_gave(ws, graphs, chem, genes):
    """A gene is described in genekg and only pointed at from chemkg. Asked of
    both, the rows are the bag of what each answers, in the order asked, and
    the receipt says how many each gave: that is how a row is traced to its
    graph."""
    g2 = rdflib.URIRef(EX + "gene/G2")
    handle, _ = run("describe", "chemkg", "ex:gene/G2", "--graph", "genekg", workspace=ws)
    assert handle["graphs"] == {"chemkg": 0, "genekg": 4} and handle["rows"] == 4
    assert exported(ws, handle["table"]) == set(graphs["genes"].triples((g2, None, None)))
    handle, _ = run(
        "incoming", "genekg", "ex:gene/G2", "--graph", "chemkg", "--out", "into", workspace=ws
    )
    pointed = set(graphs["chem"].triples((None, None, g2)))
    assert handle["graphs"] == {"genekg": 0, "chemkg": len(pointed)}
    receipt, _ = run("receipt", "into", workspace=ws)
    assert [s["origin"] for s in receipt["core"]["sources"]] == [genes.origin, chem.origin]
    log, _ = run("log", workspace=ws)
    assert [p["origin"] for p in log["steps"][-1]["pins"]] == [genes.origin, chem.origin]
    assert log["steps"][-1]["args"]["graphs"] == ["genekg", "chemkg"]


def test_a_templates_limit_bounds_the_read_so_the_guard_has_nothing_to_refuse(ws, monkeypatch):
    monkeypatch.setenv("KGFQ_FETCH_GUARD_ROWS", "3")
    handle, _ = run("instances", "chemkg", "ex:Chemical", "--limit", "5", workspace=ws)
    assert handle["rows"] == 5 and handle["complete"] is False and handle["limit_reached"] is True
    assert handle["approximations"] == [
        "stopped at the limit of 5 rows: which rows, not how many, is unspecified (R3)"
    ]
    # a limit that takes every row is not a limit that was reached
    handle, _ = run(
        "instances", "chemkg", "ex:Chemical", "--limit", "12", "--out", "all", workspace=ws
    )
    assert handle["rows"] == 12 and handle["complete"] is True and handle["limit_reached"] is False
    # over two graphs the limit is of the whole read
    handle, _ = run(
        *("instances", "chemkg", "ex:Chemical", "--graph", "genekg"),
        *("--limit", "4", "--out", "four"),
        workspace=ws,
    )
    assert handle["rows"] == 4 and handle["graphs"] == {"chemkg": 4, "genekg": 0}


def test_what_a_search_found_is_described_as_derived(ws):
    run("search", "chemkg", "atrazine", workspace=ws)
    run("describe", "chemkg", EX + "chem/1", workspace=ws)
    log, _ = run("log", workspace=ws)
    assert log["steps"][1]["inputs"][EX + "chem/1"] == {
        "kind": "derived",
        "table": "search_atrazine",
        "step": 1,
        "grade": "bundle",
    }
    # the describe's own values are there to follow next
    table = pq.read_table(ws / "tables" / "describe_1.parquet")
    assert EX + "gene/G2" in table["o"].to_pylist()


def test_templates_refuse_what_is_not_an_iri_or_not_there(ws):
    for argv, said in (
        (["describe", "chemkg", '"atrazine"'], "iri is an IRI"),
        (["describe", "chemkg", "nosuch:x"], "undeclared prefix"),
        (["describe", "nowhere", "ex:chem/1"], "no dataset 'nowhere'"),
        (["describe", "chemkg", "ex:chem/1", "--limit", "0"], "at least 1"),
        (["describe", "chemkg", "ex:chem/1", "--graph", "nowhere"], "no dataset 'nowhere'"),
    ):
        _, err = run(*argv, workspace=ws, expect=2)
        assert said in err, err
    log, _ = run("log", workspace=ws)
    assert log["steps"] == []
