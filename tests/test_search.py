"""``search`` (``kgfq.ops``): a name to an IRI, as a table a later step can
bind from. Against two live fixture servers."""

import io
import json
from pathlib import Path

import jsonschema
import pyarrow.parquet as pq
import pytest
import yaml

from kgfq import ops
from kgfq.cli import main
from kgfq.client.session import Session
from kgfq.workspace.store import Workspace

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
    return tmp_path


def test_search_turns_a_name_into_an_iri_and_files_the_hits(ws):
    handle, _ = run("search", "chemkg", "atrazine", workspace=ws)
    assert handle["table"] == "search_atrazine" and handle["rows"] == 1
    assert handle["hits"] == [
        {
            "subject": f"{EX}chem/1",
            "label": "atrazine",
            "matched": "atrazine",
            "predicate": "http://www.w3.org/2000/01/rdf-schema#label",
            "match_kind": handle["hits"][0]["match_kind"],
        }
    ]
    assert handle["hits"][0]["match_kind"] and handle["limit_reached"] is False
    table = pq.read_table(ws / "tables" / "search_atrazine.parquet")
    assert table.column_names == [
        f"{v}{suffix}"
        for v in ("subject", "label", "predicate", "matched", "match_kind", "score")
        for suffix in ("", "_kind", "_lang", "_datatype")
    ]
    [row] = table.to_pylist()
    assert (row["subject"], row["subject_kind"]) == (f"{EX}chem/1", "iri")
    assert (row["label"], row["label_kind"]) == ("atrazine", "literal")
    assert row["predicate_kind"] == "iri" and float(row["score"]) > 0

    receipt, _ = run("receipt", "search_atrazine", workspace=ws)
    RECEIPT.validate(receipt)
    core = receipt["core"]
    assert (core["kind"], core["shape"], core["pattern"]) == ("search", "bindings", None)
    assert core["completeness"]["complete"] is True and core["approximations"] == []
    assert receipt["detail"] == {"text": "atrazine", "limit": 20}
    log, _ = run("log", workspace=ws)
    [record] = log["steps"]
    ENVELOPE.validate(record)
    assert record["operation"] == "search" and record["args"] == {
        "graph": "chemkg",
        "text": "atrazine",
        "limit": 20,
        "out": "search_atrazine",
    }
    jsonschema.Draft202012Validator(OPERATIONS["search"]["args"]).validate(record["args"])
    assert record["receipt"] == receipt and record["outputs"][0]["table"] == "search_atrazine"
    assert OPERATIONS["search"]["implemented_by"]["kgfq"] == "search"


def test_what_search_finds_is_derived_and_can_be_bound(ws):
    """An IRI taken from a search's hits is derived from that step, where one
    typed from memory is asserted (C9's own test of the classification). And
    the hits bind the next fetch, with no IRI passing through the caller."""
    run("search", "chemkg", "atrazine", workspace=ws)
    run("fetch", "chemkg", "ex:chem/1 ex:affects ?gene", "--out", "typed", workspace=ws)
    handle, _ = run(
        *("fetch", "chemkg", "?subject ex:affects ?gene"),
        *("--bind", "search_atrazine:subject", "--out", "bound"),
        workspace=ws,
    )
    assert handle["rows"] == 2 and handle["bound"]["matched"] == 1
    log, _ = run("log", workspace=ws)
    derived = {"kind": "derived", "table": "search_atrazine", "step": 1, "grade": "bundle"}
    assert log["steps"][1]["inputs"][f"{EX}chem/1"] == derived
    assert log["steps"][1]["inputs"][f"{EX}affects"] == {"kind": "asserted"}
    assert log["steps"][2]["inputs"]["search_atrazine"] == derived


def test_a_search_at_its_limit_says_there_may_be_more(ws):
    every, _ = run("search", "genekg", "GENE", "--out", "genes", workspace=ws)
    assert every["rows"] == 7 and every["limit_reached"] is False
    assert {h["subject"] for h in every["hits"]} == {f"{EX}gene/G{i}" for i in range(1, 8)}
    some, _ = run("search", "genekg", "GENE", "--limit", "3", "--out", "three", workspace=ws)
    assert some["rows"] == 3 and some["limit_reached"] is True
    assert some["approximations"] == [
        "the first 3 hits by the server's rank; there may be more (--limit)"
    ]
    assert [h["subject"] for h in some["hits"]] == [h["subject"] for h in every["hits"][:3]]
    receipt, _ = run("receipt", "three", workspace=ws)
    assert receipt["core"]["approximations"] == some["approximations"]
    none, _ = run("search", "genekg", "zzzznothing", workspace=ws)
    assert none["rows"] == 0 and none["hits"] == [] and none["limit_reached"] is False


def test_search_asks_for_no_more_than_the_server_gives(tmp_path, genes):
    """`max_search_results` is the most a search returns, and there is no
    cursor past it. Asked for more, the operation takes what there is and says
    which limit it met."""
    workspace = Workspace(tmp_path)
    workspace.add_endpoint(genes.origin)
    session = Session(workspace)
    try:
        ctx = ops.Context(workspace, session)
        session.endpoint(genes.origin).service_descriptor()["caps"]["max_search_results"] = 2
        handle = ops.search(ctx, "genekg", "GENE", limit=5)
    finally:
        session.close()
    assert handle["rows"] == 2 and handle["requests"] == 1 and handle["limit_reached"] is True
    assert handle["approximations"] == [
        "the first 2 hits by the server's rank; there may be more, and this server gives a "
        "search at most 2 (max_search_results)"
    ]
    [record] = workspace.read_log()
    assert record["client"]["surface"] == "library" and record["actor"] is None
    assert record["args"]["limit"] == 5


def test_search_refuses_what_it_cannot_ask(ws):
    for argv, said in (
        (["chemkg", "   "], "search needs words to find"),
        (["chemkg", "atrazine", "--limit", "0"], "at least 1"),
        (["chemkg", "atrazine", "--out", "not a name"], "table name"),
        (["nowhere", "atrazine"], "no dataset 'nowhere'"),
    ):
        _, err = run("search", *argv, workspace=ws, expect=2)
        assert said in err, err
    log, _ = run("log", workspace=ws)
    assert log["steps"] == []
    assert (
        ops.table_name("search", "Diabetes mellitus, type 2") == "search_Diabetes_mellitus_type_2"
    )
    assert ops.table_name("search", "…") == "search"
