"""``kgfq sparql`` (plan §5.2) against the two fixture servers and a real
``kgf-sparql``: lint and refusal, sources from flags and the header, the
budget, the receipt and its engine-receipt pointer, the log, ASK and
CONSTRUCT, and the two ``--normalize`` rewrites."""

import hashlib
import io
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from kgfq.cli import main
from kgfq.lint import lint
from kgfq.sparql import bindings_table, normalize, requests_by_shape
from kgfq.sparql.normalize import _mask, _outer_group_open

P = """PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX ex: <http://example.org/>
"""
LABELS = P + "SELECT ?c ?l WHERE { ?c rdfs:label ?l }"
CHEM = "<http://example.org/chem/{}>"


def run(*argv, workspace, expect=0):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--workspace", str(workspace), "--json", *argv], out=out, err=err)
    assert code == expect, f"exit {code}: stderr={err.getvalue()!r} stdout={out.getvalue()[:400]!r}"
    text = out.getvalue()
    return (json.loads(text) if text.lstrip().startswith(("{", "[")) else text), err.getvalue()


@pytest.fixture
def ws(tmp_path, chem, genes, kgf_sparql):
    run("endpoints", "add", chem.origin, workspace=tmp_path)
    run("endpoints", "add", genes.origin, workspace=tmp_path)
    return tmp_path


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text)
    return path


def test_select_files_a_table_a_receipt_and_the_engine_receipt(ws, tmp_path):
    q = write(tmp_path, "labels.rq", LABELS)
    handle, _ = run("sparql", str(q), "--dataset", "chemkg", workspace=ws)
    assert handle["contract"] == "complete" and handle["rows"] == 12
    assert handle["grade"] == "bundle" and handle["table"] == "labels"
    assert handle["columns"] == [
        *("c", "c_kind", "c_lang", "c_datatype"),
        *("l", "l_kind", "l_lang", "l_datatype"),
    ]
    table = pq.read_table(ws / "tables" / "labels.parquet")
    assert set(table.column("c_kind").to_pylist()) == {"iri"}
    assert set(table.column("l_kind").to_pylist()) == {"literal"}
    assert set(table.column("l_lang").to_pylist()) == {"en"}
    assert table.column("c_datatype").to_pylist() == [None] * 12
    receipt, _ = run("receipt", "labels", workspace=ws)
    assert receipt["schema"] == "kgf.receipt/1.0" and receipt["core"]["kind"] == "sparql"
    [source] = receipt["core"]["sources"]
    assert source["dataset"] == "chemkg" and source["version"] == "2026-09-01"
    assert (
        source["kind"] == "kgf" and source["url"].endswith("/fragment") and source["content_digest"]
    )
    assert (
        receipt["detail"]["outcome"]["contract"] == "complete"
        and receipt["detail"]["outcome"]["rows"] == 12
    )
    assert (
        receipt["core"]["totals"]["requests"] >= 2
        and receipt["core"]["completeness"]["complete"] is True
    )
    assert receipt["core"]["completeness"]["incomplete_because"] == []
    # the engine's request list is not copied: it is filed whole beside the table and pointed at
    assert "requests" not in receipt
    assert (
        receipt["detail"]["requests_by_shape"]
        and receipt["detail"]["requests_by_shape"][0]["requests"] >= 1
    )
    engine = ws / receipt["detail"]["engine_receipt"]["path"]
    assert engine == ws / "tables" / "labels.kgf-sparql.receipt.json" and engine.exists()
    assert (
        hashlib.sha256(engine.read_bytes()).hexdigest()
        == receipt["detail"]["engine_receipt"]["sha256"]
    )
    doc = json.loads(engine.read_text())
    assert doc["schema"] == "kgf-sparql.receipt/1" == receipt["detail"]["engine_receipt"]["schema"]
    assert all(r["request_id"] for r in doc["requests"] if r["status"] == 200)
    assert (
        receipt["detail"]["engine"]["kgf_sparql"] and receipt["detail"]["engine"]["exit_code"] == 0
    )
    assert receipt["detail"]["query"] == LABELS and receipt["detail"]["original_query"] is None
    assert (
        receipt["detail"]["budget"]["max_requests"] == 20000
        and receipt["detail"]["budget"]["timeout_s"] == 60.0
    )
    assert receipt["detail"]["lint"] == {"threshold": 6, "diagnostics": []}
    assert (
        json.loads(pq.read_schema(ws / "tables" / "labels.parquet").metadata[b"kgf.receipt"])
        == receipt
    )
    log, _ = run("log", workspace=ws)
    [record] = log["steps"]
    assert record["operation"] == "sparql" and record["status"] == "ok"
    assert record["args"]["graphs"] == ["chemkg"] and record["args"]["file"] == str(q)
    assert record["pins"][0]["dataset"] == "chemkg"
    # the record carries the receipt the footer does; the engine's request
    # list stays in the engine's own receipt, pointed at
    assert record["receipt"] == receipt and "requests" not in record["receipt"]["core"]
    assert record["result"] == {"contract": "complete"}
    assert record["inputs"][str(q)]["kind"] == "external"
    [wrote] = record["outputs"]
    assert wrote["content_hash"] == handle["content_hash"] and wrote["shape"] == "bindings"


def test_sources_from_the_header_join_across_two_origins(ws, tmp_path):
    q = write(
        tmp_path,
        "affects.rq",
        "#+ summary: chemicals and the labels of the genes they affect\n#+ tags:\n#+   - chemkg\n#+   - genekg\n"
        + P
        + "SELECT ?c ?g ?gl WHERE { ?c ex:affects ?g . ?g rdfs:label ?gl }",
    )
    handle, _ = run("sparql", str(q), workspace=ws)
    assert handle["contract"] == "complete" and handle["rows"] == 24
    receipt, _ = run("receipt", "affects", workspace=ws)
    assert [s["dataset"] for s in receipt["core"]["sources"]] == ["chemkg", "genekg"]
    assert len({s["origin"] for s in receipt["core"]["sources"]}) == 2
    assert receipt["core"]["totals"]["bindings_requests"] >= 1
    pins, _ = run("pins", workspace=ws)
    assert set(pins) == {"chemkg", "genekg"}


def test_no_sources_is_a_refusal(ws, tmp_path):
    q = write(tmp_path, "bare.rq", LABELS)
    out, err = io.StringIO(), io.StringIO()
    assert main(["--workspace", str(ws), "--json", "sparql", str(q)], out=out, err=err) == 2
    assert "no sources" in err.getvalue() and "--dataset" in err.getvalue()


def test_abort_at_a_budget_is_a_receipt_without_a_table(ws, tmp_path):
    q = write(tmp_path, "labels.rq", LABELS)
    handle, _ = run(
        "sparql",
        str(q),
        "--dataset",
        "chemkg",
        "--max-requests",
        "1",
        "--out",
        "capped",
        workspace=ws,
        expect=3,
    )
    assert handle["contract"] == "aborted" and handle["budget"] == "max_requests"
    assert handle["table"] is None and handle["requests"] == 1
    assert not (ws / "tables" / "capped.parquet").exists()
    receipt, _ = run("receipt", "capped", workspace=ws)
    assert (
        receipt["detail"]["outcome"]["contract"] == "aborted"
        and receipt["detail"]["outcome"]["budget"] == "max_requests"
    )
    assert (
        receipt["core"]["completeness"]["complete"] is False
        and receipt["detail"]["engine_receipt"]["path"]
    )
    assert receipt["core"]["completeness"]["incomplete_because"] == ["max_requests"]
    assert receipt["detail"]["engine"]["exit_code"] == 3
    log, _ = run("log", workspace=ws)
    [record] = log["steps"]
    assert record["status"] == "ok" and record["outputs"] == []
    assert record["result"] == {"contract": "aborted", "rows": 0, "budget": "max_requests"}
    assert record["receipt"] == receipt, "a step with no table keeps its receipt in its record"
    # a rerun that completes replaces the receipt and writes the table
    handle, _ = run("sparql", str(q), "--dataset", "chemkg", "--out", "capped", workspace=ws)
    assert handle["rows"] == 12 and (ws / "tables" / "capped.parquet").exists()
    # and an aborted rerun of a name that has a table removes it, so a name means one thing
    handle, _ = run(
        "sparql",
        str(q),
        "--dataset",
        "chemkg",
        "--max-requests",
        "1",
        "--out",
        "capped",
        workspace=ws,
        expect=3,
    )
    assert not (ws / "tables" / "capped.parquet").exists()
    log, _ = run("log", workspace=ws)
    assert log["steps"][-1]["result"]["replaced_table"] == "capped"


def test_row_budget_writes_the_rows_with_an_approximation(ws, tmp_path):
    q = write(tmp_path, "labels.rq", LABELS)
    handle, _ = run(
        "sparql",
        str(q),
        "--dataset",
        "chemkg",
        "--max-rows",
        "3",
        "--out",
        "few",
        workspace=ws,
        expect=3,
    )
    assert (
        handle["contract"] == "aborted" and handle["budget"] == "max_rows" and handle["rows"] == 3
    )
    assert "max-rows 3" in handle["approximations"][0]
    assert pq.read_table(ws / "tables" / "few.parquet").num_rows == 3
    receipt, _ = run("receipt", "few", workspace=ws)
    assert (
        receipt["core"]["completeness"]["complete"] is False and receipt["core"]["approximations"]
    )
    assert receipt["core"]["completeness"]["incomplete_because"] == ["max_rows"]


def test_refusal_above_the_threshold_or_at_a_strong_diagnostic_unless_forced(ws, tmp_path):
    product = write(
        tmp_path, "product.rq", P + "SELECT ?a ?l WHERE { ?a rdfs:label ?l . ?b ex:affects ?g }"
    )
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--workspace", str(ws), "--json", "sparql", str(product), "--dataset", "chemkg"],
        out=out,
        err=err,
    )
    assert code == 2 and "refused: KGFQ004" in err.getvalue() and "--force" in err.getvalue()
    assert "KGFQ004 [strong]" in err.getvalue()
    log, _ = run("log", workspace=ws)
    assert log["steps"][-1]["status"] == "error" and log["steps"][-1]["error"].startswith("refused")
    handle, _ = run("sparql", str(product), "--dataset", "chemkg", "--force", workspace=ws)
    assert handle["rows"] == 12 * 24 and handle["lint"] == ["KGFQ004"]
    two = write(
        tmp_path, "two.rq", P + "SELECT ?c ?g ?l WHERE { ?c ex:affects ?g . ?c rdfs:label ?l }"
    )
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "--workspace",
            str(ws),
            "--json",
            "sparql",
            str(two),
            "--dataset",
            "chemkg",
            "--threshold",
            "1",
        ],
        out=out,
        err=err,
    )
    assert code == 2 and "refused: KGFQ008" in err.getvalue()
    broken = write(tmp_path, "broken.rq", "SELECT ?x WHERE { ?x ns:p ?y }")
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--workspace", str(ws), "--json", "sparql", str(broken), "--dataset", "chemkg"],
        out=out,
        err=err,
    )
    assert code == 2 and "parse error" in err.getvalue()
    log, _ = run("log", workspace=ws)
    assert log["steps"][-1]["status"] == "error" and log["steps"][-1]["error"].startswith(
        "parse error"
    )


def test_engine_error_is_verbatim_and_a_named_endpoint_is_graded_sparql(ws, tmp_path):
    q = write(tmp_path, "labels.rq", LABELS)
    handle, err = run(
        "sparql",
        str(q),
        "--sparql-endpoint",
        "http://127.0.0.1:9/sparql",
        "--out",
        "nowhere",
        workspace=ws,
        expect=1,
    )
    assert handle["contract"] == "error" and handle["grade"] == "sparql" and handle["error"]
    assert err.strip() == handle["error"].strip()
    receipt, _ = run("receipt", "nowhere", workspace=ws)
    assert receipt["core"]["sources"] == [
        {"kind": "sparql", "grade": "sparql", "url": "http://127.0.0.1:9/sparql"}
    ]
    assert (
        receipt["detail"]["outcome"]["contract"] == "error"
        and receipt["detail"]["outcome"]["error"] == handle["error"]
    )
    log, _ = run("log", workspace=ws)
    assert log["steps"][-1]["status"] == "error" and log["steps"][-1]["error"] == handle["error"]


def test_ask_and_construct(ws, tmp_path):
    ask = write(tmp_path, "ask.rq", P + f"ASK {{ {CHEM.format(1)} rdfs:label ?l }}")
    handle, _ = run("sparql", str(ask), "--dataset", "chemkg", workspace=ws)
    assert (
        handle["contract"] == "complete" and handle["boolean"] is True and handle["table"] is None
    )
    assert (ws / "tables" / "ask.receipt.json").exists() and not (
        ws / "tables" / "ask.parquet"
    ).exists()
    log, _ = run("log", workspace=ws)
    # the engine counts the answer as one row; the receipt keeps its numbers verbatim
    assert log["steps"][-1]["result"] == {"contract": "complete", "rows": 1, "boolean": True}
    construct = write(
        tmp_path, "cons.rq", P + "CONSTRUCT { ?c ex:hasLabel ?l } WHERE { ?c rdfs:label ?l }"
    )
    handle, _ = run("sparql", str(construct), "--dataset", "chemkg", workspace=ws)
    assert handle["rows"] == 12 and handle["columns"] == [
        *("s", "p", "o"),
        *("o_kind", "o_lang", "o_datatype"),
    ]
    table = pq.read_table(ws / "tables" / "cons.parquet")
    assert set(table.column("o_kind").to_pylist()) == {"literal"}
    assert set(table.column("p").to_pylist()) == {"http://example.org/hasLabel"}
    assert set(table.column("o_lang").to_pylist()) == {"en"}


NORM = (
    P + f"SELECT ?c ?g WHERE {{ BIND({CHEM.format(1)} AS ?x) ?x ex:affects ?g . ?c ex:affects ?g . "
    f"FILTER(?c = {CHEM.format(4)} || ?c IN ({CHEM.format(6)})) }}"
)


def test_normalize_records_equivalent_rewrites_and_the_answer_is_unchanged(ws, tmp_path):
    q = write(tmp_path, "norm.rq", NORM)
    plain, _ = run("sparql", str(q), "--dataset", "chemkg", "--out", "plain", workspace=ws)
    normed, _ = run(
        "sparql", str(q), "--dataset", "chemkg", "--out", "normed", "--normalize", workspace=ws
    )
    assert plain["rows"] == normed["rows"] == 3 and plain["content_hash"] == normed["content_hash"]
    assert plain["lint"] == ["KGFQ001", "KGFQ002"] and normed["lint"] == []
    assert [(n["rule"], n["applied"]) for n in normed["normalizations"]] == [
        ("KGFQ002", True),
        ("KGFQ001", True),
    ]
    receipt, _ = run("receipt", "normed", workspace=ws)
    assert receipt["detail"]["original_query"] == NORM and "BIND" not in receipt["detail"]["query"]
    assert f"VALUES ?x {{ {CHEM.format(1)} }}" in receipt["detail"]["query"]
    assert f"VALUES ?c {{ {CHEM.format(4)} {CHEM.format(6)} }}" in receipt["detail"]["query"]
    assert "FILTER" in receipt["detail"]["query"], "the filter stays as the residual check"
    log, _ = run("log", workspace=ws)
    applied = log["steps"][-1]["receipt"]["detail"]["normalizations"]
    assert [n["rule"] for n in applied if n["applied"]] == ["KGFQ002", "KGFQ001"]


def test_normalize_declines_what_it_cannot_prove():
    typed = P + 'SELECT ?c WHERE { ?c ex:mass ?m . FILTER(?m = "107.5"^^xsd:double) }'
    text, records = normalize(typed, lint(typed))
    assert text == typed and [r["applied"] for r in records] == [False]
    assert "by value" in records[0]["reason"]
    across = P + "SELECT ?c ?g WHERE { ?c ex:affects ?g . FILTER(?c = ex:A || ?g = ex:B) }"
    text, records = normalize(across, lint(across))
    assert text == across and all(not r["applied"] for r in records) and len(records) == 2
    negated = P + "SELECT ?c ?g WHERE { ?c ex:affects ?g . FILTER(!(?c = ex:A)) }"
    text, records = normalize(negated, lint(negated))
    assert text == negated and [r["applied"] for r in records] == [False]
    # a conjunction with an unrelated constraint is fine; the VALUES restricts no more than the filter
    conj = (
        P
        + 'SELECT ?c ?l WHERE { ?c rdfs:label ?l . FILTER((?c = ex:A || ?c = ex:B) && LANG(?l) = "en") }'
    )
    text, records = normalize(conj, lint(conj))
    assert (
        records[0]["applied"]
        and "VALUES ?c { <http://example.org/A> <http://example.org/B> }" in text
    )
    assert lint(text) == []
    # comments and strings that contain braces do not move the insertion point
    tricky = (
        P
        + '# not a group {\nSELECT ?c ?l WHERE { ?c rdfs:label ?l . FILTER(?c = ex:A && ?l != "{") }'
    )
    text, records = normalize(tricky, lint(tricky))
    assert records[0]["applied"] and text.index("VALUES ?c") > text.index("WHERE {")
    assert lint(text) == []


def test_mask_and_outer_group():
    text = 'PREFIX ex: <http://x/#a>\nSELECT ?a # { c\nWHERE { ?a ex:p "}" }'
    masked = _mask(text)
    assert len(masked) == len(text) and "# { c" not in masked and '"}"' not in masked
    assert masked[text.index("<http://x/#a>") :].startswith("<http://x/#a>")
    at = _outer_group_open(text, "SelectQuery")
    assert at is not None and text[at] == "{" and at > text.index("WHERE")
    construct = "CONSTRUCT { ?a ex:q ?b } WHERE { ?a ex:p ?b }"
    assert _outer_group_open(construct, "ConstructQuery") == construct.rindex("{")
    assert _outer_group_open("CONSTRUCT { ?a ex:q ?b }", "ConstructQuery") is None


def test_engine_missing_is_a_polite_refusal(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("KGFQ_KGF_SPARQL", str(tmp_path / "nope.js"))
    q = write(tmp_path, "labels.rq", LABELS)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--workspace", str(ws), "--json", "sparql", str(q), "--dataset", "chemkg"],
        out=out,
        err=err,
    )
    assert (
        code == 2
        and "npm install -g kgf-sparql" in err.getvalue()
        and "is not a file" in err.getvalue()
    )
    assert not (ws / "kgfq.log.jsonl").exists(), "nothing was attempted, so nothing is logged"


def test_requests_by_shape_folds_the_corpus_shape():
    base = "https://apps.okn.us/kgf/spoke-okn/v/v0.0.5"
    label = "%3Chttp%3A%2F%2Fwww.w3.org%2F2000%2F01%2Frdf-schema%23label%3E"
    requests = (
        [
            {
                "method": "GET",
                "url": f"{base}/count?s=%3Chttp%3A%2F%2Fx%2F{i}%3E&p={label}",
                "status": 200,
                "bytes": 100,
                "elapsed_ms": 1.0,
                "bindings": False,
            }
            for i in range(3)
        ]
        + [
            {
                "method": "GET",
                "url": f"{base}/fragment?s=%3Chttp%3A%2F%2Fx%2F{i}%3E&p={label}",
                "status": 200,
                "bytes": 500,
                "elapsed_ms": 2.5,
                "bindings": False,
            }
            for i in range(2)
        ]
        + [
            {
                "method": "QUERY",
                "url": f"{base}/fragment",
                "status": None,
                "bytes": 0,
                "elapsed_ms": 0.5,
                "bindings": True,
                "error": "aborted",
            },
            {
                "method": "GET",
                "url": "https://apps.okn.us/kgf/spoke-okn",
                "status": 200,
                "bytes": 1496,
                "elapsed_ms": 80.0,
                "bindings": False,
            },
        ]
    )
    shapes = requests_by_shape(requests)
    assert [
        (s["method"], s["url"].rsplit("/", 1)[-1], s["bound"], s["requests"]) for s in shapes
    ] == [
        ("GET", "count", "sp", 3),
        ("GET", "fragment", "sp", 2),
        ("GET", "spoke-okn", "", 1),
        ("QUERY", "fragment", "", 1),
    ]
    assert (
        shapes[0]["predicate"] == "<http://www.w3.org/2000/01/rdf-schema#label>"
        and shapes[0]["bytes"] == 300
    )
    assert shapes[1]["elapsed_ms"] == 5.0 and shapes[1]["statuses"] == {"200": 2}
    assert (
        shapes[3]["bindings"] is True and shapes[3]["failed"] == 1 and shapes[3]["statuses"] == {}
    )


def test_bindings_table_encoding_tells_iris_from_strings():
    doc = {
        "head": {"vars": ["a", "b"]},
        "results": {
            "bindings": [
                {
                    "a": {"type": "uri", "value": "http://x/1"},
                    "b": {"type": "literal", "value": "plain"},
                },
                {
                    "a": {"type": "bnode", "value": "b0"},
                    "b": {"type": "literal", "value": "hi", "xml:lang": "en"},
                },
                {
                    "a": {
                        "type": "literal",
                        "value": "1",
                        "datatype": "http://www.w3.org/2001/XMLSchema#integer",
                    }
                },
            ]
        },
    }
    table = bindings_table(doc)
    assert table.column_names == [
        *("a", "a_kind", "a_lang", "a_datatype"),
        *("b", "b_kind", "b_lang", "b_datatype"),
    ]
    assert table.column("a").to_pylist() == ["http://x/1", "_:b0", "1"]
    assert table.column("a_kind").to_pylist() == ["iri", "bnode", "literal"]
    assert table.column("a_datatype").to_pylist() == [
        None,
        None,
        "http://www.w3.org/2001/XMLSchema#integer",
    ]
    assert table.column("b").to_pylist() == ["plain", "hi", None]
    assert table.column("b_kind").to_pylist() == ["literal", "literal", None]
    assert table.column("b_lang").to_pylist() == [None, "en", None]
    # a plain literal stores no datatype: its kind says it is a literal, RDF 1.1 says xsd:string
    assert table.column("b_datatype").to_pylist() == [None, None, None]
