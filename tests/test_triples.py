"""A table back to its triples (C6): the round trip a fetched table's receipt
makes possible, against a live fixture server and the file it was built from."""

import io
import json
from pathlib import Path

import pyarrow as pa
import pytest
import rdflib

from kgfq.cli import main
from kgfq.client.terms import TRIPLE_COLUMNS
from kgfq.workspace.store import Workspace, WorkspaceError
from kgfq.workspace.triples import triples, write_ntriples

FIXTURES = Path(__file__).parent / "fixtures"
EX = "http://example.org/"
AFFECTS = rdflib.URIRef(EX + "affects")
LABEL = rdflib.RDFS.label


def run(*argv, workspace, expect=0):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--workspace", str(workspace), "--json", *argv], out=out, err=err)
    assert code == expect, f"exit {code}: stderr={err.getvalue()!r}"
    return out.getvalue(), err.getvalue()


def graph(text: str) -> set:
    g = rdflib.Graph()
    g.parse(data=text, format="nt")
    return set(g)


@pytest.fixture(scope="module")
def source() -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(FIXTURES / "chem.nt", format="nt")
    return g


@pytest.fixture
def ws(tmp_path, chem):
    run("endpoints", "add", chem.origin, workspace=tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("pattern", "match"),
    [
        # every position a variable: the whole graph, under s, p and o
        ("? ? ?", (None, None, None)),
        # named variables, a constant predicate that has no column
        ("?c ex:affects ?g", (None, AFFECTS, None)),
        # literals with a language tag, anonymous positions
        ("? rdfs:label ?", (None, LABEL, None)),
        # typed literals, a variable predicate
        ("ex:chem/1 ?p ?o", (rdflib.URIRef(EX + "chem/1"), None, None)),
        # one variable: two constants put back
        ("?c ex:affects ex:gene/G2", (None, AFFECTS, rdflib.URIRef(EX + "gene/G2"))),
    ],
)
def test_an_exported_fetch_is_the_triples_the_graph_holds(ws, source, pattern, match):
    run("fetch", "chemkg", pattern, "--out", "t", workspace=ws)
    out, _ = run("export", "t", workspace=ws)
    expected = set(source.triples(match))
    assert expected and graph(out) == expected
    assert len(out.splitlines()) == len(expected), "one line a row, none repeated"


def test_export_writes_a_file_and_is_not_a_step(ws, source):
    run("fetch", "chemkg", "? ? ?", "--out", "everything", workspace=ws)
    path = ws / "out" / "everything.nt"
    out, _ = run("export", "everything", "--out", str(path), workspace=ws)
    assert json.loads(out) == {
        "table": "everything",
        "format": "nt",
        "triples": len(source),
        "path": str(path),
    }
    assert graph(path.read_text(encoding="utf-8")) == set(source)
    assert [r["command"] for r in Workspace(ws).read_log()] == ["fetch"]


def test_a_bound_fetch_exports_the_rows_its_values_reached(ws, source, tmp_path):
    values = tmp_path / "chems.tsv"
    values.write_text("?c\n<http://example.org/chem/1>\n<http://example.org/chem/2>\n")
    run("fetch", "chemkg", "?c ex:affects ?g", "--values", str(values), "--out", "b", workspace=ws)
    out, _ = run("export", "b", workspace=ws)
    chems = {rdflib.URIRef(f"{EX}chem/{i}") for i in (1, 2)}
    assert graph(out) == {t for t in source.triples((None, AFFECTS, None)) if t[0] in chems}


def cells(rows):
    return pa.table(
        {c: pa.array(col, type=pa.string()) for c, col in zip(TRIPLE_COLUMNS, rows, strict=True)}
    )


def test_a_triples_table_is_its_own_answer_and_other_shapes_have_none():
    table = cells([["http://x/a"], ["http://x/p"], ["v"], ["literal"], [None], [None]])
    assert triples(table, {"shape": "triples"}).equals(table)
    select = pa.table(
        {"a": ["http://x/a"], "a_kind": ["iri"], "a_lang": [None], "a_datatype": [None]}
    )
    for receipt in (
        {"shape": "bindings", "pattern": None, "kind": "sparql", "table": "hits"},
        {"kind": "derived", "table": "hits"},
    ):
        with pytest.raises(WorkspaceError, match="hits answers no single pattern"):
            triples(select, receipt)
    with pytest.raises(WorkspaceError, match=r"names \?b, and it has no column b, b_kind"):
        triples(
            select,
            {"shape": "bindings", "pattern": {"s": "?a", "p": "<http://x/p>", "o": "?b"}},
        )


def test_ntriples_spells_every_kind_of_term():
    table = cells(
        [
            ["http://x/a", "_:b0", "http://x/a b", "http://x/a", "http://x/a"],
            ["http://x/p"] * 5,
            ['say "hi"\nthere\\', "_:b1", "http://x/o", "42", "chat"],
            ["literal", "bnode", "iri", "literal", "literal"],
            [None, None, None, None, "fr"],
            [None, None, None, "http://www.w3.org/2001/XMLSchema#integer", None],
        ]
    )
    out = io.StringIO()
    assert write_ntriples(table, out) == 5
    lines = out.getvalue().splitlines()
    assert lines == [
        '<http://x/a> <http://x/p> "say \\"hi\\"\\nthere\\\\" .',
        "_:b0 <http://x/p> _:b1 .",
        "<http://x/a\\u0020b> <http://x/p> <http://x/o> .",
        '<http://x/a> <http://x/p> "42"^^<http://www.w3.org/2001/XMLSchema#integer> .',
        '<http://x/a> <http://x/p> "chat"@fr .',
    ]
    parsed = rdflib.Graph()
    parsed.parse(data=out.getvalue(), format="nt")
    assert len(parsed) == 5
    assert rdflib.Literal('say "hi"\nthere\\') in set(parsed.objects())
