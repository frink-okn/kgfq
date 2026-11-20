"""``labels`` (``kgfq.ops``): one label per IRI in a table's column, as a table
to join. Against two live fixture servers: chemkg names chemicals and points at
genes, and the genes' names are in genekg."""

import io
import json
from pathlib import Path

import jsonschema
import pyarrow.parquet as pq
import pytest
import yaml

from kgfq.cli import main

EX = "http://example.org/"
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
    run("fetch", "chemkg", "?chem ex:affects ?gene", "--out", "affects", workspace=tmp_path)
    return tmp_path


def rows(ws, name):
    table = pq.read_table(ws / "tables" / f"{name}.parquet")
    return {r["iri"]: (r["label"], r["graph"]) for r in table.to_pylist()}


def test_labels_are_a_table_from_the_graph_the_column_came_from(ws, chem):
    handle, _ = run("labels", "affects", "chem", workspace=ws)
    assert handle["table"] == "labels_affects_chem" and handle["graphs"] == ["chemkg"]
    assert handle["rows"] == handle["labeled"] == 12 and handle["unlabeled"] == 0
    got = rows(ws, "labels_affects_chem")
    assert got[f"{EX}chem/1"] == ("atrazine", "chemkg") and len(got) == 12
    table = pq.read_table(ws / "tables" / "labels_affects_chem.parquet")
    assert table.column_names == [
        f"{v}{s}" for v in ("iri", "label", "graph") for s in ("", "_kind", "_lang", "_datatype")
    ]
    assert set(table["iri_kind"].to_pylist()) == {"iri"}
    assert set(table["label_kind"].to_pylist()) == {"literal"}

    receipt, _ = run("receipt", "labels_affects_chem", workspace=ws)
    RECEIPT.validate(receipt)
    core = receipt["core"]
    assert (core["kind"], core["shape"], core["pattern"]) == ("labels", "bindings", None)
    assert [s["origin"] for s in core["sources"]] == [chem.origin]
    assert core["inputs"]["affects"] == {
        "kind": "derived",
        "table": "affects",
        "step": 1,
        "grade": "bundle",
    }
    assert core["bindings"]["sent"] == 12 and core["bindings"]["unmatched"] == 0
    log, _ = run("log", workspace=ws)
    record = log["steps"][-1]
    ENVELOPE.validate(record)
    assert record["operation"] == "labels" and record["args"] == {
        "table": "affects",
        "column": "chem",
        "out": "labels_affects_chem",
    }
    jsonschema.Draft202012Validator(OPERATIONS["labels"]["args"]).validate(record["args"])
    assert record["receipt"] == receipt and record["inputs"]["affects"]["step"] == 1


def test_the_names_are_often_in_another_graph_and_the_handle_says_so(ws, chem, genes):
    """chemkg points at five genes and names none of them. Asked alone, it
    labels nothing, and the handle says five are unlabeled with a sample;
    genekg has the names. Asked in order, the first graph with a label wins."""
    alone, _ = run("labels", "affects", "gene", workspace=ws)
    assert alone["graphs"] == ["chemkg"] and alone["labeled"] == 0 and alone["unlabeled"] == 5
    assert sorted(alone["unlabeled_sample"]) == [f"<{EX}gene/G{i}>" for i in range(1, 6)]
    assert set(rows(ws, "labels_affects_gene").values()) == {(None, None)}

    both, _ = run(
        *("labels", "affects", "gene", "--graph", "chemkg", "--graph", "genekg"),
        *("--out", "gene_names"),
        workspace=ws,
    )
    assert both["graphs"] == ["chemkg", "genekg"] and both["labeled"] == 5
    assert rows(ws, "gene_names")[f"{EX}gene/G2"] == ("GENE 2", "genekg")
    receipt, _ = run("receipt", "gene_names", workspace=ws)
    assert [s["origin"] for s in receipt["core"]["sources"]] == [chem.origin, genes.origin]
    log, _ = run("log", workspace=ws)
    record = log["steps"][-1]
    assert record["args"]["graphs"] == ["chemkg", "genekg"]
    assert [p["origin"] for p in record["pins"]] == [chem.origin, genes.origin]
    # the second graph is asked only for what the first did not label
    asked = [q for q in receipt["core"]["requests"] if "/labels" in q["url"]]
    assert [q["rows"] for q in asked] == [5, 5]


def test_labels_refuses_what_has_no_iri_to_label(ws):
    run("fetch", "chemkg", "?chem rdfs:label ?name", "--out", "names", workspace=ws)
    for argv, said in (
        (["names", "name"], "holds no IRI to label: its 12 values are literals or blank nodes"),
        (["names", "nothing"], "has no column 'nothing'"),
        (["nowhere", "chem"], "no table 'nowhere' to bind from"),
        (["affects", "gene", "--graph", "nowhere"], "no dataset 'nowhere'"),
    ):
        _, err = run("labels", *argv, workspace=ws, expect=2)
        assert said in err, err
    log, _ = run("log", workspace=ws)
    assert [r["operation"] for r in log["steps"]] == ["fetch", "fetch"]
