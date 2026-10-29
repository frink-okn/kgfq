"""The CLI skin against two live fixture servers."""

import io
import json
import subprocess
import sys

import pyarrow.parquet as pq
import pytest

from kgfq.cli import _tokenize, main


def run(*argv, workspace, expect=0):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--workspace", str(workspace), "--json", *argv], out=out, err=err)
    assert code == expect, f"exit {code}: stderr={err.getvalue()!r} stdout={out.getvalue()[:400]!r}"
    return (
        json.loads(out.getvalue())
        if out.getvalue().lstrip().startswith(("{", "["))
        else out.getvalue()
    ), err.getvalue()


@pytest.fixture
def ws(tmp_path, chem, genes):
    run("endpoints", "add", chem.origin, workspace=tmp_path)
    run("endpoints", "add", genes.origin, workspace=tmp_path)
    return tmp_path


def test_table_forces_text_when_piped(ws):
    out = io.StringIO()
    assert main(["--workspace", str(ws), "--table", "inventory", "genekg"], out=out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0].split()[:3] == ["class", "predicate", "entities"]
    assert lines[1].split()[:2] == ["http://example.org/Gene", "http://example.org/inPathway"]
    assert not out.getvalue().lstrip().startswith("{")
    trailing = io.StringIO()
    assert main(["--workspace", str(ws), "inventory", "genekg", "--table"], out=trailing) == 0
    assert trailing.getvalue() == out.getvalue()


def test_tokenize_keeps_literals_and_iris_whole():
    assert _tokenize('?s rdfs:label "Diabetes mellitus"@en') == [
        "?s",
        "rdfs:label",
        '"Diabetes mellitus"@en',
    ]
    assert _tokenize("<http://x/a b> ? ?o") == ["<http://x/a b>", "?", "?o"]


def test_endpoints_and_datasets(ws, chem, genes):
    payload, _ = run("endpoints", "list", workspace=ws)
    assert payload["endpoints"] == [chem.origin, genes.origin]
    payload, _ = run("datasets", workspace=ws)
    assert [(d["id"], d["origin"]) for d in payload["datasets"]] == [
        ("chemkg", chem.origin),
        ("genekg", genes.origin),
    ]
    payload, _ = run("datasets", "--text", "pathways", workspace=ws)
    assert [d["id"] for d in payload["datasets"]] == ["genekg"]


def test_summary_schema_count_sample(ws):
    md, _ = run("summary", "chemkg", workspace=ws)
    assert "Chem KG" in md
    payload, _ = run(
        "schema", "chemkg", "--children", "properties", "--limit", "2", "--all", workspace=ws
    )
    assert len(payload["items"]) >= 4 and payload["complete"] is True
    payload, _ = run("count", "chemkg", "? ex:affects ?", workspace=ws)
    assert payload == {"count": 24, "exact": True, "source": "inventory:predicate"}
    payload, _ = run("count", "chemkg", '? rdfs:label "atrazine"@en', workspace=ws)
    assert payload["count"] == 1
    payload, _ = run("sample", "chemkg", "?c ex:affects ?g", "-n", "3", "--seed", "1", workspace=ws)
    assert payload["vars"] == ["c", "g"] and len(payload["rows"]) == 3


def test_count_and_fetch_with_values_file(ws, tmp_path):
    values = tmp_path / "chems.tsv"
    values.write_text("?c\n<http://example.org/chem/1>\n<http://example.org/chem/2>\nex:chem/3\n")
    payload, _ = run("count", "chemkg", "?c ex:affects ?g", "--values", str(values), workspace=ws)
    assert payload["total"] == 6 and [c["count"] for c in payload["counts"]] == [2, 2, 2]
    payload, _ = run("explain", "chemkg", "?c ex:affects ?g", "--values", str(values), workspace=ws)
    assert payload["steps"][-1]["op"] == "bindings" and payload["evidence"]["r2"]["k"] == 3
    handle, _ = run(
        "fetch",
        "chemkg",
        "?c ex:affects ?g",
        "--values",
        str(values),
        "--out",
        "affects3",
        workspace=ws,
    )
    assert handle["rows"] == 6 and handle["columns"] == [
        "s",
        "p",
        "o",
        "o_kind",
        "o_lang",
        "o_datatype",
        "binding",
    ]
    assert handle["grade"] == "bundle" and handle["complete"] is True
    receipt, _ = run("receipt", "affects3", workspace=ws)
    assert receipt["inputs"][str(values)]["kind"] == "external"
    assert receipt["plan"]["steps"][-1]["op"] == "bindings"
    assert receipt["totals"]["requests"] >= 1 and all(r["request_id"] for r in receipt["requests"])


def test_fetch_max_rows_is_an_approximation_only_when_rows_remained(ws):
    handle, _ = run(
        "fetch", "chemkg", "?c ex:affects ?g", "--max-rows", "5", "--out", "few", workspace=ws
    )
    assert handle["rows"] == 5 and handle["complete"] is False
    assert "--max-rows 5" in handle["approximations"][0]
    receipt, _ = run("receipt", "few", workspace=ws)
    assert receipt["schema"] == "kgfq.receipt/4"
    assert receipt["completeness"]["incomplete_because"] == ["max_rows"]
    handle, _ = run(
        "fetch", "chemkg", "?c ex:affects ?g", "--max-rows", "24", "--out", "all", workspace=ws
    )
    assert handle["rows"] == 24 and handle["complete"] is True and handle["approximations"] == []


def test_fetch_logs_and_classifies_inputs(ws, tmp_path):
    handle, _ = run("fetch", "chemkg", "? rdfs:label ?", workspace=ws)
    assert handle["table"] == "label" and handle["rows"] == 12
    assert (ws / "tables" / "label.parquet").exists() and (
        ws / "tables" / "label.receipt.json"
    ).exists()
    # a constant that came out of the previous fetch is derived; a fresh one is asserted
    _, _ = run(
        "fetch", "chemkg", "<http://example.org/chem/1> ex:affects ?", "--out", "c1", workspace=ws
    )
    _, _ = run("count", "genekg", "<http://example.org/gene/G9> ? ?", workspace=ws)
    log, _ = run("log", workspace=ws)
    steps = {r["step"]: r for r in log["steps"]}
    assert [r["command"] for r in log["steps"]] == ["fetch", "fetch", "count"]
    assert steps[2]["inputs"]["http://example.org/chem/1"] == {
        "kind": "derived",
        "table": "label",
        "step": 1,
        "grade": "bundle",
    }
    assert steps[3]["inputs"]["http://example.org/gene/G9"] == {"kind": "asserted"}
    assert steps[1]["pins"][0]["dataset"] == "chemkg" and steps[1]["receipt"]["grade"] == "bundle"
    assert steps[1]["output"]["content_hash"] == handle["content_hash"]


def test_fetch_to_csv_path_with_receipt_beside_it(ws, tmp_path):
    out = tmp_path / "exports" / "labels.csv"
    handle, _ = run("fetch", "chemkg", "? rdfs:label ?", "--out", str(out), workspace=ws)
    assert out.exists() and out.with_name("labels.receipt.json").exists()
    assert out.read_text().splitlines()[0] == "s,p,o,o_kind,o_lang,o_datatype"
    handle, _ = run(
        "fetch", "chemkg", "? rdfs:label ?", "--out", "few", "--max-rows", "5", workspace=ws
    )
    assert handle["rows"] == 5 and handle["approximations"] and "R3" in handle["approximations"][0]


def test_inventory_is_one_ranked_table_from_cached_reads(ws):
    """C12: the three reads once, the table ranked, absolute IRIs, and on
    --out a Parquet file with its receipt and a log step."""
    payload, _ = run("inventory", "genekg", workspace=ws)
    assert payload["reads"] == {
        "classes": "server",
        "class-properties": "server",
        "class-relations": "server",
    }
    assert payload["view"] == "queryable" and payload["columns"][:2] == ["class", "predicate"]
    rows = payload["rows"]
    assert [r["entities"] for r in rows] == sorted((r["entities"] for r in rows), reverse=True)
    pathway = next(r for r in rows if r["predicate"] == "http://example.org/inPathway")
    assert pathway["class"] == "http://example.org/Gene"
    assert (pathway["entities"], pathway["distinct_subjects"], pathway["triples"]) == (7, 7, 7)
    assert (pathway["coverage"], pathway["fanout"]) == (1.0, 1.0)
    assert pathway["object_classes"] == "http://example.org/Pathway"

    handle, _ = run("inventory", "genekg", "--out", "inv", workspace=ws)
    assert handle["requests"] == 0 and set(handle["reads"].values()) == {"cache"}
    assert handle["rows"] == len(rows) and handle["grade"] == "bundle"
    table = pq.read_table(ws / "tables" / "inv.parquet")
    assert table.column_names == payload["columns"]
    assert table.column("predicate").to_pylist()[: len(rows)] == [r["predicate"] for r in rows]
    receipt, _ = run("receipt", "inv", workspace=ws)
    assert receipt["kind"] == "inventory" and receipt["intent"]["reads"] == handle["reads"]
    text = io.StringIO()  # the trailing flag must not shadow receipt's positional
    assert main(["--workspace", str(ws), "receipt", "inv", "--table"], out=text) == 0
    assert text.getvalue().startswith("{")  # a receipt has no table form; JSON still
    assert receipt["completeness"]["complete"] is True and receipt["requests"] == []
    log, _ = run("log", workspace=ws)
    assert [s["command"] for s in log["steps"]][-2:] == ["inventory", "inventory"]
    assert log["steps"][-1]["output"]["table"] == "inv"

    # the same projection through `schema --all` now reads the cache too
    schema, _ = run("schema", "genekg", "--projection", "class-relations", "--all", workspace=ws)
    assert schema["view"] == "queryable" and schema["complete"] is True
    assert {i["predicate"]["value"] for i in schema["items"]} == {"http://example.org/inPathway"}
    assert log_requests(ws) == 0


GENES_CONFIG = """
targets:
  gene:
    type: http://example.org/Gene
    include_predicates: ['http://example.org/symbol', 'http://example.org/inPathway']
  pathway:
    type: http://example.org/Pathway
    include_predicates: ['http://example.org/nosuch']
"""


def test_verbalize_runs_a_config_and_files_the_plan(ws, tmp_path):
    """C13: the config goes to the server verbatim; the plan comes back as
    per-target figures and sampled texts; `unknown[]` names what the config
    declared that the graph lacks; n is held to the cap; --out files the plan."""
    config = tmp_path / "genes.verbalize.yaml"
    config.write_text(GENES_CONFIG)
    payload, _ = run(
        "verbalize", "genekg", "--config", str(config), "-n", "2", "--seed", "1", workspace=ws
    )
    assert payload["mode"] == "plan" and payload["n"] == 2 and payload["seed"] == 1
    assert payload["unknown"] == [
        {"at": "targets.pathway.include_predicates", "iri": "http://example.org/nosuch"}
    ]
    by_name = {t["name"]: t for t in payload["targets"]}
    assert (by_name["gene"]["members"], by_name["gene"]["sampled"]) == (7, 2)
    assert by_name["pathway"]["members"] == 3
    gene_records = [r for r in payload["records"] if r["target"] == "gene"]
    assert len(gene_records) == 2
    assert all(
        "symbol: G" in r["text"] and "in pathway: Pathway" in r["text"] for r in gene_records
    )
    assert all(r["iri"]["value"].startswith("http://example.org/gene/") for r in gene_records)

    # one root by IRI, under its target: records only, nothing to file
    payload, _ = run(
        "verbalize",
        "genekg",
        "--config",
        str(config),
        "--target",
        "gene",
        "--iri",
        "http://example.org/gene/G1",
        workspace=ws,
    )
    assert payload["mode"] == "iris" and [r["label"] for r in payload["records"]] == ["GENE 1"]
    _, err = run(
        "verbalize",
        "genekg",
        "--config",
        str(config),
        "--target",
        "gene",
        "--iri",
        "http://example.org/gene/G1",
        "--out",
        "x",
        workspace=ws,
        expect=2,
    )
    assert "mode 'iris' has none" in err

    # the root cap is the server's rule over the whole plan (targets x n): verbatim
    _, err = run("verbalize", "genekg", "--config", str(config), "-n", "60", workspace=ws, expect=2)
    problem = json.loads(err)
    assert problem["code"] == "cap_exceeded" and "lower n or name one target" in problem["detail"]

    # --out: the targets as a table, the receipt naming the config as an input
    payload, _ = run("verbalize", "genekg", "--config", str(config), "--out", "plan", workspace=ws)
    assert payload["table"]["rows"] == 2
    table = pq.read_table(ws / "tables" / "plan.parquet")
    assert table.column("name").to_pylist() == ["gene", "pathway"]
    assert set(table.column_names) >= {"members", "sampled", "mean_chars", "truncated"}
    receipt, _ = run("receipt", "plan", workspace=ws)
    assert receipt["kind"] == "verbalize" and receipt["intent"]["mode"] == "plan"
    assert receipt["intent"]["config"] == str(config)
    assert receipt["inputs"][str(config)]["kind"] == "external"
    assert receipt["completeness"]["complete"] is True and receipt["totals"]["requests"] == 1
    assert receipt["approximations"] == []

    # no config and a bundle that publishes none: the server's problem document, verbatim
    _, err = run("verbalize", "genekg", workspace=ws, expect=2)
    problem = json.loads(err)
    assert problem["code"] == "malformed_request" and "config is required" in problem["detail"]


def log_requests(ws) -> int:
    """Requests the last log step made."""
    log, _ = run("log", "--last", "1", workspace=ws)
    receipt = log["steps"][-1].get("receipt") or {}
    return receipt.get("requests", receipt.get("totals", {}).get("requests", 0))


def test_errors_are_the_servers_problem_document(ws, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--workspace", str(ws), "--json", "count", "chemkg", "? nosuch:x ?"], out=out, err=err
    )
    assert code == 2 and "kgfq:" in err.getvalue() and "undeclared prefix" in err.getvalue()
    out, err = io.StringIO(), io.StringIO()
    code = main(["--workspace", str(ws), "--json", "count", "nowhere", "? ? ?"], out=out, err=err)
    assert code == 2 and "no dataset 'nowhere'" in err.getvalue()
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--workspace", str(ws), "--json", "--endpoint", "http://127.0.0.1:9", "datasets"],
        out=out,
        err=err,
    )
    assert code == 0 and "unreachable" in err.getvalue()


def test_console_script_runs():
    result = subprocess.run(
        [sys.executable, "-m", "kgfq.cli", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and "fetch" in result.stdout
