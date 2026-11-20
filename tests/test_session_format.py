"""The shared session format (``notes/session-format.md``): what kgfq writes
validates against the schemas in ``docs/session/``, and what another client
wrote is read without being understood."""

import io
import json
from pathlib import Path

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kgfq.cli import main
from kgfq.workspace.log import classify_values
from kgfq.workspace.store import RECEIPT_KEY, Workspace
from kgfq.workspace.triples import triples

SESSION = Path(__file__).parent.parent / "docs" / "session"
RECEIPT = jsonschema.Draft202012Validator(json.loads((SESSION / "receipt.schema.json").read_text()))
P = "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\nPREFIX ex: <http://example.org/>\n"


def run(*argv, workspace, expect=0):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--workspace", str(workspace), "--json", *argv], out=out, err=err)
    assert code == expect, f"exit {code}: stderr={err.getvalue()!r}"
    return json.loads(out.getvalue())


def receipts(ws: Path) -> dict[str, dict]:
    """Every receipt the workspace holds: each table's footer, and each sidecar,
    which must say what the footer says where there is a table."""
    found: dict[str, dict] = {}
    for sidecar in sorted((ws / "tables").glob("*.receipt.json")):
        if sidecar.name.endswith(".kgf-sparql.receipt.json"):
            continue  # the engine's own receipt, in the engine's schema
        found[sidecar.name.removesuffix(".receipt.json")] = json.loads(sidecar.read_text())
    for table in sorted((ws / "tables").glob("*.parquet")):
        footer = json.loads(pq.read_schema(table).metadata[RECEIPT_KEY])
        assert found.get(table.stem, footer) == footer
        found[table.stem] = footer
    return found


def test_fetch_receipts_validate(tmp_path, chem):
    ws = tmp_path
    run("endpoints", "add", chem.origin, workspace=ws)
    values = tmp_path / "chems.tsv"
    values.write_text("?c\n<http://example.org/chem/1>\n<http://example.org/chem/2>\n")
    run("fetch", "chemkg", "? rdfs:label ?", "--out", "labels", workspace=ws)
    run("fetch", "chemkg", "?c ex:affects ?g", "--values", str(values), "--out", "b", workspace=ws)
    run("fetch", "chemkg", "?c ex:affects ?g", "--max-rows", "3", "--out", "few", workspace=ws)
    found = receipts(ws)
    assert sorted(found) == ["b", "few", "labels"]
    for name, receipt in found.items():
        RECEIPT.validate(receipt)
        core = receipt["core"]
        assert core["kind"] == "fetch" and core["shape"] == "bindings" and core["table"] == name
        assert core["sources"][0]["origin"] == chem.origin and core["requests"]
        assert set(receipt["detail"]) == {"intent", "plan"}
    assert found["few"]["core"]["completeness"]["incomplete_because"] == ["max_rows"]
    assert found["b"]["core"]["totals"]["bindings_requests"] == 1


def test_sparql_receipts_validate_and_state_their_shape(tmp_path, chem, kgf_sparql):
    ws = tmp_path
    run("endpoints", "add", chem.origin, workspace=ws)
    queries = {
        "sel": "SELECT ?c ?l WHERE { ?c rdfs:label ?l }",
        "cons": "CONSTRUCT { ?c ex:hasLabel ?l } WHERE { ?c rdfs:label ?l }",
        "ask": "ASK { ?c rdfs:label ?l }",
    }
    for name, text in queries.items():
        (tmp_path / f"{name}.rq").write_text(P + text)
        run("sparql", str(tmp_path / f"{name}.rq"), "--dataset", "chemkg", workspace=ws)
    run(
        *("sparql", str(tmp_path / "sel.rq"), "--dataset", "chemkg"),
        *("--max-requests", "1", "--out", "capped"),
        workspace=ws,
        expect=3,
    )
    found = receipts(ws)
    for receipt in found.values():
        RECEIPT.validate(receipt)
        assert receipt["core"]["kind"] == "sparql" and "requests" not in receipt["core"]
        assert receipt["detail"]["engine_receipt"]["path"]
    assert {n: r["core"]["shape"] for n, r in found.items()} == {
        "sel": "bindings",
        "cons": "triples",
        "ask": None,
        "capped": None,
    }
    assert found["sel"]["core"]["pattern"] is None
    assert found["capped"]["core"]["completeness"]["incomplete_because"] == ["max_requests"]
    assert "table" not in found["capped"]["core"]


def test_another_clients_receipt_is_read_without_being_understood(tmp_path):
    """An operation kgfq does not implement, a grade, a source kind and fields it
    has never seen: the table still loads, its grade still carries, its rows are
    still triples by its pattern, and its detail comes back as it was written."""
    ws = Workspace(tmp_path)
    foreign = {
        "schema": "kgf.receipt/1.7",
        "core": {
            "kind": "overlap",
            "step": 4,
            "grade": "bundle",
            "sources": [{"kind": "kgf", "grade": "bundle", "dataset": "a", "mirror": True}],
            "shape": "bindings",
            "pattern": {"s": "?ns", "p": "<http://x/sharedWith>", "o": "?other"},
            "completeness": {"complete": True, "incomplete_because": [], "checked": 7},
            "totals": {"requests": 3, "cache_hits": 2},
            "created": "2026-11-20T15:02:11Z",
            "table": "t4",
            "written_by_a_newer_client": {"anything": 1},
        },
        "detail": {"namespaces": ["http://x/"], "skipped": {"vocabularies": 5}},
        "also_new": True,
    }
    RECEIPT.validate(foreign)
    table = pa.table(
        {
            c: pa.array(v, type=pa.string())
            for c, v in {
                "ns": ["http://x/"],
                "ns_kind": ["iri"],
                "ns_lang": [None],
                "ns_datatype": [None],
                "other": ["http://y/"],
                "other_kind": ["iri"],
                "other_lang": [None],
                "other_datatype": [None],
            }.items()
        }
    )
    ws.tables_dir.mkdir()
    meta = {RECEIPT_KEY: json.dumps(foreign).encode()}
    pq.write_table(table.replace_schema_metadata(meta), ws.table_path("t4"))
    read = ws.read_receipt("t4")
    assert read == foreign
    assert classify_values(ws, ["http://y/", "http://z/"]) == {
        "http://y/": {"kind": "derived", "table": "t4", "step": 4, "grade": "bundle"},
        "http://z/": {"kind": "asserted"},
    }
    assert triples(table, read).to_pylist() == [
        {
            "s": "http://x/",
            "p": "http://x/sharedWith",
            "o": "http://y/",
            "o_kind": "iri",
            "o_lang": None,
            "o_datatype": None,
        }
    ]


def test_the_receipt_schema_closes_only_the_major_version():
    receipt = {
        "schema": "kgf.receipt/1.0",
        "core": {
            "kind": "fetch",
            "grade": "bundle",
            "sources": [],
            "completeness": {"complete": True, "incomplete_because": []},
            "totals": {"requests": 0},
            "created": "2026-11-20T15:02:11Z",
        },
    }
    RECEIPT.validate(receipt)
    RECEIPT.validate({**receipt, "schema": "kgf.receipt/1.12"})
    for other in ("kgf.receipt/2.0", "kgfq.receipt/4", "kgf.receipt/1"):
        with pytest.raises(jsonschema.ValidationError):
            RECEIPT.validate({**receipt, "schema": other})
    with pytest.raises(jsonschema.ValidationError, match="'grade' is a required property"):
        RECEIPT.validate(
            {**receipt, "core": {k: v for k, v in receipt["core"].items() if k != "grade"}}
        )
