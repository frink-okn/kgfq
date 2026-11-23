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
import yaml

from kgfq.cli import Cli, main
from kgfq.workspace.log import classify_values
from kgfq.workspace.store import RECEIPT_KEY, Workspace
from kgfq.workspace.triples import triples

SESSION = Path(__file__).parent.parent / "docs" / "session"
RECEIPT = jsonschema.Draft202012Validator(json.loads((SESSION / "receipt.schema.json").read_text()))
ENVELOPE = jsonschema.Draft202012Validator(
    json.loads((SESSION / "envelope.schema.json").read_text())
)
OPERATIONS = yaml.safe_load((SESSION / "operations.yaml").read_text())["operations"]
CLIENTS = {"kgfq", "kgf-explorer"}
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


def check_log(ws: Path) -> list[dict]:
    """Every record is an envelope, names a registered operation that kgfq is
    said to implement, carries arguments the registry's schema accepts, and a
    receipt that validates and is the one its table carries."""
    records = [json.loads(line) for line in (ws / "kgfq.log.jsonl").read_text().splitlines()]
    assert [r["step"] for r in records] == list(range(1, len(records) + 1))
    filed = receipts(ws)
    for r in records:
        ENVELOPE.validate(r)
        entry = OPERATIONS[r["operation"]]
        assert "kgfq" in entry["implemented_by"], r["operation"]
        jsonschema.Draft202012Validator(entry["args"]).validate(r["args"])
        assert set(r["args"]) <= set(entry["args"]["properties"]), (r["operation"], r["args"])
        assert r["client"]["name"] == "kgfq" and r["actor"] is None
        RECEIPT.validate(r["receipt"])
        assert r["receipt"]["core"]["kind"] == r["operation"]
        assert r["receipt"]["core"]["step"] == r["step"]
        assert [p["origin"] for p in r["pins"]] == [
            s["origin"] for s in r["receipt"]["core"]["sources"] if s["kind"] == "kgf"
        ]
        for wrote in r["outputs"]:
            core = r["receipt"]["core"]
            assert wrote == {
                k: core[k] for k in ("table", "shape", "rows", "grade", "content_hash")
            }
    # the latest step to write a name is the one whose receipt the table carries
    latest = {o["table"]: r["receipt"] for r in records for o in r["outputs"]}
    assert all(filed[name] == receipt for name, receipt in latest.items())
    return records


def test_every_step_kgfq_logs_is_an_envelope_in_the_registrys_spelling(tmp_path, chem, genes):
    ws = tmp_path
    run("endpoints", "add", chem.origin, workspace=ws)
    run("endpoints", "add", genes.origin, workspace=ws)
    values = tmp_path / "chems.tsv"
    values.write_text("?c\n<http://example.org/chem/1>\n<http://example.org/chem/2>\n")
    run("list_graphs", workspace=ws)
    main(["--workspace", str(ws), "summary", "chemkg"], out=io.StringIO(), err=io.StringIO())
    run("schema", "chemkg", "--class", "ex:Chemical", "--children", "properties", workspace=ws)
    run("count", "chemkg", "? ex:affects ?", workspace=ws)
    run("count", "chemkg", "?c ex:affects ?g", "--values", str(values), workspace=ws)
    run("sample", "genekg", "?g ex:inPathway ?p", "-n", "3", workspace=ws)
    run("distinct", "chemkg", "? ex:affects ?g", "--by", "o", "--top", "2", workspace=ws)
    run("fetch", "chemkg", "?c ex:affects ?g", "--values", str(values), "--out", "b", workspace=ws)
    run("fetch", "genekg", '?g rdfs:label "GENE 1"@en', "--max-rows", "5", workspace=ws)
    records = check_log(ws)
    assert [(r["operation"], r["args"]) for r in records] == [
        ("list_graphs", {}),
        ("graph_summary", {"graph": "chemkg", "format": "md"}),
        (
            "schema",
            {"graph": "chemkg", "class": "http://example.org/Chemical", "children": "properties"},
        ),
        ("count", {"graphs": ["chemkg"], "predicate": "<http://example.org/affects>"}),
        (
            "count",
            {
                "graphs": ["chemkg"],
                "subject": "?c",
                "predicate": "<http://example.org/affects>",
                "object": "?g",
                "values": str(values),
            },
        ),
        (
            "sample",
            {
                "graph": "genekg",
                "subject": "?g",
                "predicate": "<http://example.org/inPathway>",
                "object": "?p",
                "n": 3,
                "seed": 0,
            },
        ),
        (
            "distinct",
            {
                "graph": "chemkg",
                "predicate": "<http://example.org/affects>",
                "object": "?g",
                "by": "o",
                "top": 2,
            },
        ),
        (
            "fetch",
            {
                "graph": "chemkg",
                "subject": "?c",
                "predicate": "<http://example.org/affects>",
                "object": "?g",
                "values": str(values),
                "out": "b",
            },
        ),
        (
            "fetch",
            {
                "graph": "genekg",
                "subject": "?g",
                "predicate": "<http://www.w3.org/2000/01/rdf-schema#label>",
                "object": '"GENE 1"@en',
                "max_rows": 5,
                "out": "label_GENE_1",
            },
        ),
    ]
    # two graphs on two servers: each step names the origin it read
    assert {r["pins"][0]["origin"] for r in records[1:]} == {chem.origin, genes.origin}
    assert [len(r["outputs"]) for r in records] == [0, 0, 0, 0, 0, 0, 0, 1, 1]
    assert records[3]["result"] == {"count": 24, "exact": True, "source": "inventory:predicate"}
    assert records[3]["receipt"]["core"]["totals"]["requests"] >= 1
    # the listing read no release: nothing pinned, and its sources are the origins
    assert records[0]["pins"] == [] and records[0]["result"]["graphs"] == 2
    assert {s["kind"] for s in records[0]["receipt"]["core"]["sources"]} == {"origin"}


def test_a_sparql_step_is_an_envelope_too(tmp_path, chem, kgf_sparql):
    ws = tmp_path
    run("endpoints", "add", chem.origin, workspace=ws)
    (tmp_path / "sel.rq").write_text(P + "SELECT ?c ?l WHERE { ?c rdfs:label ?l }")
    run("sparql", str(tmp_path / "sel.rq"), "--dataset", "chemkg", workspace=ws)
    run(
        *("sparql", str(tmp_path / "sel.rq"), "--dataset", "chemkg"),
        *("--max-requests", "1", "--out", "capped"),
        workspace=ws,
        expect=3,
    )
    done, capped = check_log(ws)
    assert done["outputs"][0]["table"] == "sel" and done["result"] == {"contract": "complete"}
    assert capped["outputs"] == [] and capped["result"]["budget"] == "max_requests"
    assert capped["receipt"] == receipts(ws)["capped"]


def test_another_clients_record_is_an_envelope_kgfq_can_read():
    """What the explorer would write for an operation kgfq does not have: it
    validates, and everything a reader needs to show the step, find its table
    and carry its grade is in fields kgfq understands."""
    record = {
        "schema": "kgf.session/1.3",
        "step": 4,
        "started": "2026-11-20T15:02:11Z",
        "elapsed_ms": 812,
        "client": {"name": "kgf-explorer", "version": "df21dc2", "surface": "page"},
        "actor": "assistant",
        "operation": "overlap",
        "args": {"graphs": ["sawgraph", "biobricks-toxcast"]},
        "inputs": {},
        "pins": [
            {
                "origin": "https://apps.okn.us/kgf",
                "dataset": "sawgraph",
                "version": "2026-04-02",
                "content_digest": "sha256:0",
            }
        ],
        "outputs": [{"table": "t4", "shape": "bindings", "rows": 12, "grade": "bundle"}],
        "status": "ok",
        "labels_fetched": 40,
    }
    ENVELOPE.validate(record)
    assert "kgfq" not in OPERATIONS[record["operation"]]["implemented_by"]
    jsonschema.Draft202012Validator(OPERATIONS["overlap"]["args"]).validate(record["args"])
    for schema in ("kgf.session/2.0", "kgfq.log/1"):
        with pytest.raises(jsonschema.ValidationError):
            ENVELOPE.validate({**record, "schema": schema})
    with pytest.raises(jsonschema.ValidationError, match="'operation' is a required property"):
        ENVELOPE.validate({k: v for k, v in record.items() if k != "operation"})


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


def test_the_registry_is_well_formed():
    """Each operation says what it means, takes arguments a JSON Schema describes,
    and is implemented by at least one client under a name of that client's."""
    doc = yaml.safe_load((SESSION / "operations.yaml").read_text())
    assert doc["schema"] == "kgf.operations/1.0" and len(OPERATIONS) >= 30
    for name, entry in OPERATIONS.items():
        assert name.isidentifier() and name == name.lower(), name
        assert len(entry["meaning"].split()) >= 4, name
        jsonschema.Draft202012Validator.check_schema(entry["args"])
        assert entry["args"]["type"] == "object", name
        assert entry["args"].get("additionalProperties", True), f"{name}: nothing is closed"
        assert entry["implemented_by"] and set(entry["implemented_by"]) <= CLIENTS, name
        assert entry.get("table") in (None, "bindings", "triples"), name
        for required in entry["args"].get("required", []):
            assert required in entry["args"]["properties"], f"{name} requires {required}"


def test_every_operation_kgfq_claims_is_a_command_it_has():
    """The registry's ``implemented_by`` is the parity record (§8), so a name
    kgfq is said to have must be a command its skin runs."""
    claimed = {
        e["implemented_by"]["kgfq"] for e in OPERATIONS.values() if "kgfq" in e["implemented_by"]
    }
    assert all(callable(getattr(Cli, name, None)) for name in claimed), claimed
    assert claimed == {
        *("list_graphs", "graph_summary", "schema", "count"),
        *("sample", "distinct", "fetch", "sparql", "search", "labels"),
        *("describe", "incoming", "instances", "neighbors", "sql"),
    }
    # where kgfq has an operation it goes by the registry's name
    assert all(e["implemented_by"].get("kgfq", name) == name for name, e in OPERATIONS.items())
