"""The CLI skin against two live fixture servers."""

import io
import json
import subprocess
import sys

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
    assert receipt["schema"] == "kgfq.receipt/3"
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


def test_values_the_pattern_does_not_name_are_refused(ws, tmp_path):
    """2026-11-06: a `?paper` header against a bare `?` subject bound nothing, so
    `count` reported the whole predicate once per row and `fetch` fetched it once
    per row. Now count, explain and fetch refuse it, and nothing is logged or written."""
    values = tmp_path / "chems.tsv"
    values.write_text("?chem\n<http://example.org/chem/1>\n<http://example.org/chem/2>\n")
    for command in ("count", "explain", "fetch"):
        _, err = run(
            command, "chemkg", "? ex:affects ?g", "--values", str(values), workspace=ws, expect=2
        )
        assert err.startswith("kgfq: binding variable ?chem does not appear in the pattern")
        assert "whose variables are ?g" in err
        assert "`?chem <http://example.org/affects> ?g`" in err
    both = tmp_path / "both.json"
    both.write_text(json.dumps({"vars": ["?c", "?x"], "rows": [["ex:chem/1", '"a"']]}))
    _, err = run(
        "fetch", "chemkg", "?c ex:affects ?g", "--values", str(both), workspace=ws, expect=2
    )
    assert "?x does not appear" in err and "whose variables are ?c, ?g." in err
    log, _ = run("log", workspace=ws)
    assert log["steps"] == []
    assert not (ws / "tables" / "affects.parquet").exists()
