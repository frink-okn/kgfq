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


def test_a_command_goes_by_its_registry_name_and_its_old_one(ws, chem):
    """`list_graphs` and `graph_summary` are the session format's names for
    what kgfq first called `datasets` and `summary`; both spellings run, and
    so does the hyphenated one."""
    listed = [run(name, workspace=ws)[0] for name in ("list_graphs", "list-graphs", "datasets")]
    assert listed[0] == listed[1] == listed[2] and len(listed[0]["datasets"]) == 2
    cards = [
        run(name, "chemkg", workspace=ws)[0]
        for name in ("graph_summary", "graph-summary", "summary")
    ]
    assert cards[0] == cards[1] == cards[2] and "Chem KG" in cards[0]
    log, _ = run("log", workspace=ws)
    assert {r["operation"] for r in log["steps"]} == {"graph_summary"}


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
        *("c", "c_kind", "c_lang", "c_datatype"),
        *("g", "g_kind", "g_lang", "g_datatype"),
    ]
    assert handle["grade"] == "bundle" and handle["complete"] is True
    receipt, _ = run("receipt", "affects3", workspace=ws)
    assert receipt["core"]["inputs"][str(values)]["kind"] == "external"
    assert receipt["detail"]["plan"]["steps"][-1]["op"] == "bindings"
    assert receipt["core"]["shape"] == "bindings" and receipt["core"]["pattern"] == {
        "s": "?c",
        "p": "<http://example.org/affects>",
        "o": "?g",
    }
    assert receipt["core"]["totals"]["requests"] >= 1 and all(
        r["request_id"] for r in receipt["core"]["requests"]
    )


def test_fetch_max_rows_is_an_approximation_only_when_rows_remained(ws):
    handle, _ = run(
        "fetch", "chemkg", "?c ex:affects ?g", "--max-rows", "5", "--out", "few", workspace=ws
    )
    assert handle["rows"] == 5 and handle["complete"] is False
    assert "--max-rows 5" in handle["approximations"][0]
    receipt, _ = run("receipt", "few", workspace=ws)
    assert receipt["schema"] == "kgf.receipt/1.0"
    assert receipt["core"]["completeness"]["incomplete_because"] == ["max_rows"]
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
    assert [r["operation"] for r in log["steps"]] == ["fetch", "fetch", "count"]
    assert steps[2]["inputs"]["http://example.org/chem/1"] == {
        "kind": "derived",
        "table": "label",
        "step": 1,
        "grade": "bundle",
    }
    assert steps[3]["inputs"]["http://example.org/gene/G9"] == {"kind": "asserted"}
    assert steps[1]["pins"][0]["dataset"] == "chemkg"
    assert steps[1]["args"] == {
        "graph": "chemkg",
        "predicate": "<http://www.w3.org/2000/01/rdf-schema#label>",
        "out": "label",
    }
    [wrote] = steps[1]["outputs"]
    assert wrote == {
        "table": "label",
        "shape": "bindings",
        "rows": 12,
        "grade": "bundle",
        "content_hash": handle["content_hash"],
    }
    # the record carries the receipt the table's footer does
    assert steps[1]["receipt"] == run("receipt", "label", workspace=ws)[0]
    assert steps[3]["args"] == {"graphs": ["genekg"], "subject": "<http://example.org/gene/G9>"}
    assert steps[3]["outputs"] == [] and steps[3]["result"]["count"] == 0
    assert steps[3]["receipt"]["core"]["kind"] == "count"


def test_fetch_to_csv_path_with_receipt_beside_it(ws, tmp_path):
    out = tmp_path / "exports" / "labels.csv"
    handle, _ = run("fetch", "chemkg", "? rdfs:label ?", "--out", str(out), workspace=ws)
    assert out.exists() and out.with_name("labels.receipt.json").exists()
    assert (
        out.read_text().splitlines()[0] == "s,s_kind,s_lang,s_datatype,o,o_kind,o_lang,o_datatype"
    )
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


def test_distinct_lists_values_with_counts(ws, tmp_path):
    # `? ex:affects ?g` grouped by object: each gene with the chemicals'
    # triples that reach it, summing to the pattern's count.
    payload, _ = run("distinct", "chemkg", "? ex:affects ?g", "--by", "o", workspace=ws)
    assert payload["group_by"] == "o" and payload["triples"] == 24
    assert sum(r["count"] for r in payload["rows"]) == 24
    assert payload["distinct"] == len(payload["rows"])
    assert len({r["value"] for r in payload["rows"]}) == payload["distinct"]

    # The same answer one value a page.
    paged, _ = run(
        "distinct", "chemkg", "? ex:affects ?g", "--by", "o", "--limit", "1", workspace=ws
    )
    assert paged["rows"] == payload["rows"]

    # A predicate's subjects, the pair no permutation keeps contiguous: one
    # row per chemical, with how many genes it affects.
    subjects, _ = run("distinct", "chemkg", "?c ex:affects ?", "--by", "s", workspace=ws)
    assert subjects["triples"] == 24 and sum(r["count"] for r in subjects["rows"]) == 24

    # Under bindings: the predicates three chemicals use, and how many of the
    # three reach each one.
    values = tmp_path / "chems.tsv"
    values.write_text("?c\n<http://example.org/chem/1>\n<http://example.org/chem/2>\nex:chem/3\n")
    payload, _ = run(
        "distinct", "chemkg", "?c ? ?", "--by", "p", "--values", str(values), workspace=ws
    )
    rows = {r["value"]: r for r in payload["rows"]}
    affects = rows["<http://example.org/affects>"]
    assert affects["count"] == 6 and affects["bindings"] == 3
    assert payload["triples"] == sum(r["count"] for r in payload["rows"])

    # --top prints the largest values and still reports the whole answer.
    top, _ = run("distinct", "chemkg", "? ? ?", "--by", "p", "--top", "1", workspace=ws)
    assert len(top["rows"]) == 1 and top["distinct"] > 1
    assert top["rows"][0]["count"] == max(
        r["count"] for r in run("distinct", "chemkg", "? ? ?", "--by", "p", workspace=ws)[0]["rows"]
    )


def test_distinct_refuses_a_bound_position_before_any_request(ws):
    # A bound position has one value; refused here, before any request.
    _, err = run("distinct", "chemkg", "? ex:affects ?", "--by", "p", workspace=ws, expect=2)
    assert "binds p" in err


def test_distinct_refuses_an_equality_get_cannot_carry(ws):
    # `?c ex:affects ?c` over GET would lose the equality; refused before any
    # request rather than answered as `? ex:affects ?`.
    _, err = run("distinct", "chemkg", "?c ex:affects ?c", "--by", "s", workspace=ws, expect=2)
    assert "repeats ?c" in err


def test_an_anonymous_position_does_not_join_a_variable_named_like_it(ws, tmp_path):
    # The caller's `?o` binds the subject. The anonymous object must stay
    # independent of it, not become `?o` too and ask for chemicals that affect
    # themselves.
    values = tmp_path / "chems.tsv"
    values.write_text("?o\n<http://example.org/chem/1>\n<http://example.org/chem/2>\n")
    payload, _ = run(
        "distinct", "chemkg", "?o ex:affects ?", "--by", "o", "--values", str(values), workspace=ws
    )
    assert payload["triples"] == 4
    assert sum(r["count"] for r in payload["rows"]) == 4


def test_count_and_sample_keep_an_anonymous_position_apart_too(ws, tmp_path):
    # `?o ex:affects ?`: the subject is called `o` and the object has no name.
    # The body spelled the object `?o` as well, so a bound count asked for
    # chemicals that affect themselves; and rows were keyed `o` for both
    # positions, so a subject came back as its object.
    values = tmp_path / "chems.tsv"
    values.write_text("?o\n<http://example.org/chem/1>\n<http://example.org/chem/2>\n")
    payload, _ = run("count", "chemkg", "?o ex:affects ?", "--values", str(values), workspace=ws)
    assert [c["count"] for c in payload["counts"]] == [2, 2]
    payload, _ = run("sample", "chemkg", "?o ex:affects ?", "-n", "5", workspace=ws)
    assert payload["vars"] == ["o", "_kgfq_o"]
    assert all("/chem/" in r["o"] and "/gene/" in r["_kgfq_o"] for r in payload["rows"])


def test_a_fetched_tables_columns_are_the_patterns_variables(ws, tmp_path):
    # A constant has no column: it is in the receipt's pattern. An anonymous
    # position's column is the position's own name.
    handle, _ = run("fetch", "chemkg", "?chem ex:affects ?", "--out", "named", workspace=ws)
    assert handle["rows"] == 24 and handle["columns"] == [
        *("chem", "chem_kind", "chem_lang", "chem_datatype"),
        *("o", "o_kind", "o_lang", "o_datatype"),
    ]
    table = pq.read_table(ws / "tables" / "named.parquet")
    assert set(table["chem_kind"].to_pylist()) == {"iri"} == set(table["o_kind"].to_pylist())
    assert all("/chem/" in c for c in table["chem"].to_pylist())
    receipt, _ = run("receipt", "named", workspace=ws)
    assert receipt["core"]["pattern"] == {
        "s": "?chem",
        "p": "<http://example.org/affects>",
        "o": "?o",
    }
    # Every column needs a name of its own, and it is the caller's to choose.
    for pattern, said in (
        ("?o ex:affects ?", "leaves its object unnamed, and that column would be `o`"),
        ("ex:chem/1 ex:affects ex:gene/G1", "has no variable"),
        ("?c ex:affects ?c_kind", "would both need the column `c_kind`"),
    ):
        for command in ("explain", "fetch"):
            _, err = run(command, "chemkg", pattern, workspace=ws, expect=2)
            assert said in err, err
    _, err = run("fetch", "chemkg", "?o ex:affects ?", workspace=ws, expect=2)
    assert err.rstrip().endswith("e.g. `?o <http://example.org/affects> ?object`.")
    log, _ = run("log", workspace=ws)
    assert [r["operation"] for r in log["steps"]] == ["fetch"]


def test_count_fetch_and_sample_refuse_an_equality_get_cannot_carry(ws):
    # `?c ex:affects ?c` matches nothing; over GET it was `? ex:affects ?`.
    for command in ("count", "explain", "fetch", "sample"):
        _, err = run(command, "chemkg", "?c ex:affects ?c", workspace=ws, expect=2)
        assert err.startswith("kgfq: `?c <http://example.org/affects> ?c` repeats ?c")
    log, _ = run("log", workspace=ws)
    assert log["steps"] == []
    assert not (ws / "tables").exists()
