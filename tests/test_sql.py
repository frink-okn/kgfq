"""``sql`` (``kgfq.ops.sql``) and the sandboxed DuckDB behind it
(``kgfq.workspace.sandbox``; plan C8, D6). Against two live fixture servers for
the tables, and against the sandbox for what it must refuse."""

import datetime
import decimal
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rdflib
import yaml

from kgfq.cli import main
from kgfq.client.terms import Iri, Literal
from kgfq.ops import compact
from kgfq.workspace import sandbox
from kgfq.workspace.receipts import new_receipt
from kgfq.workspace.store import Workspace

EX = "http://example.org/"
XSD = "http://www.w3.org/2001/XMLSchema#"
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
    for graph, pattern, name in (
        ("chemkg", "?chem ex:affects ?gene", "affects"),
        ("genekg", "?gene ex:inPathway ?pathway", "pathways"),
        ("chemkg", "?chem ex:mass ?mass", "mass"),
        ("chemkg", "?chem rdfs:label ?name", "names"),
    ):
        run("fetch", graph, pattern, "--out", name, workspace=tmp_path)
    return tmp_path


def rows(ws, name):
    return pq.read_table(ws / "tables" / f"{name}.parquet").to_pylist()


def test_sql_joins_two_graphs_tables_and_the_result_is_a_table_like_any_other(ws, chem, genes):
    """The join the fixture exists for: chemicals to the pathways of the genes
    they affect, across two graphs on two servers. The result's IRIs are IRIs
    again without being told, since the tables read hold them as IRIs."""
    c, g = rdflib.Graph(), rdflib.Graph()
    c.parse(FIXTURES / "chem.nt", format="nt")
    g.parse(FIXTURES / "genes.nt", format="nt")
    expected = sorted(
        (str(chem_), str(pathway))
        for chem_, _, gene in c.triples((None, rdflib.URIRef(EX + "affects"), None))
        for _, _, pathway in g.triples((gene, rdflib.URIRef(EX + "inPathway"), None))
    )
    statement = (
        "SELECT a.chem, p.pathway FROM affects a JOIN pathways p ON a.gene = p.gene "
        "ORDER BY a.chem, p.pathway"
    )
    handle, _ = run("sql", statement, "--out", "reached", workspace=ws)
    assert handle["rows"] == len(expected) == 24 and handle["columns"] == ["chem", "pathway"]
    assert handle["grade"] == "bundle" and handle["reads"] == ["affects", "pathways"]
    assert handle["complete"] is True and "note" not in handle
    got = rows(ws, "reached")
    assert [(r["chem"], r["pathway"]) for r in got] == expected
    assert {r["chem_kind"] for r in got} == {"iri"} == {r["pathway_kind"] for r in got}
    assert handle["preview"][0] == {"chem": expected[0][0], "pathway": expected[0][1]}

    receipt, _ = run("receipt", "reached", workspace=ws)
    RECEIPT.validate(receipt)
    core, detail = receipt["core"], receipt["detail"]
    assert (core["kind"], core["shape"], core["pattern"]) == ("sql", "bindings", None)
    assert core["sources"] == [] and core["totals"]["requests"] == 0
    assert core["inputs"] == {
        "affects": {"kind": "derived", "table": "affects", "step": 1, "grade": "bundle"},
        "pathways": {"kind": "derived", "table": "pathways", "step": 2, "grade": "bundle"},
    }
    assert detail["statement"] == statement and detail["reads"] == ["affects", "pathways"]
    assert detail["graphs"] == [f"chemkg@{chem.origin}", f"genekg@{genes.origin}"]
    assert detail["engine"]["duckdb"].startswith("v") and detail["plain_text"] == {}
    log, _ = run("log", workspace=ws)
    record = log["steps"][-1]
    ENVELOPE.validate(record)
    assert record["operation"] == "sql" and record["args"] == {"query": statement, "out": "reached"}
    jsonschema.Draft202012Validator(OPERATIONS["sql"]["args"]).validate(record["args"])
    assert record["pins"] == [] and record["receipt"] == receipt

    # the result carries into the next steps as any table does
    bound, _ = run(
        "fetch", "genekg", "?pathway rdfs:label ?l", "--bind", "reached:pathway", workspace=ws
    )
    assert bound["bound"]["unmatched"] == 0 and bound["rows"] == len({p for _, p in expected})
    named, _ = run("labels", "reached", "chem", "--out", "chem_names", workspace=ws)
    assert named["graphs"] == ["chemkg", "genekg"] and named["unlabeled"] == 0


def test_what_sql_computes_is_a_typed_literal_and_every_cell_is_text(ws):
    """A count, a sum, a mean, a flag: each text with its datatype beside it, so
    the table has the layout every table has and its hash needs no spelling of
    a number."""
    handle, _ = run(
        *("sql", "SELECT gene, count(*) AS n FROM affects GROUP BY gene ORDER BY n DESC, gene"),
        *("--out", "by_gene"),
        workspace=ws,
    )
    got = rows(ws, "by_gene")
    assert handle["rows"] == 5 and sum(int(r["n"]) for r in got) == 24
    assert {(r["n_kind"], r["n_datatype"]) for r in got} == {("literal", XSD + "integer")}
    # stored as text with its datatype; shown, in the handle, as the number it is
    assert handle["preview"][0]["n"] == int(got[0]["n"]) and isinstance(got[0]["n"], str)
    assert handle["content_hash"].startswith("sha256:")
    statement = (
        "SELECT sum(CAST(mass AS DOUBLE)) AS total, avg(CAST(mass AS DOUBLE)) AS mean, "
        "count(*) > 10 AS many, sum(1) AS ones, CAST(avg(CAST(mass AS DECIMAL(9,2))) AS DECIMAL(9,3)) AS d "
        "FROM mass"
    )
    run("sql", statement, "--out", "stats", workspace=ws)
    [stats] = rows(ws, "stats")
    masses = [float(r["mass"]) for r in rows(ws, "mass")]
    assert stats["total_datatype"] == XSD + "double" == stats["mean_datatype"]
    assert float(stats["total"]) == pytest.approx(sum(masses))
    assert stats["mean"] == sandbox.js_number(sum(masses) / len(masses))
    assert (stats["many"], stats["many_datatype"]) == ("true", XSD + "boolean")
    assert (stats["ones"], stats["ones_datatype"]) == ("12", XSD + "decimal")
    assert stats["d_datatype"] == XSD + "decimal" and "e" not in stats["d"].lower()
    assert all(pa.types.is_string(f.type) for f in pq.read_schema(ws / "tables" / "stats.parquet"))


def test_a_result_columns_kind_is_stated_or_found_never_guessed(ws):
    # every column and its companions: the same table, by its hash
    whole, _ = run("sql", "SELECT * FROM affects", "--out", "copy", workspace=ws)
    source, _ = run("receipt", "affects", workspace=ws)
    assert whole["columns"] == ["chem", "gene"]
    assert whole["content_hash"] == source["core"]["content_hash"]

    # a literal keeps its language, found in the table it was read from
    run("sql", "SELECT name FROM names ORDER BY name LIMIT 1", "--out", "first", workspace=ws)
    assert rows(ws, "first") == [
        {"name": "atrazine", "name_kind": "literal", "name_lang": "en", "name_datatype": None}
    ]

    # text built in SQL is text, and the handle says so and how to say otherwise
    built = "SELECT 'http://example.org/made/' || name AS made FROM names"
    handle, _ = run("sql", built, "--out", "made", workspace=ws)
    assert {r["made_kind"] for r in rows(ws, "made")} == {"literal"}
    assert handle["note"].startswith("Plain text, not IRIs: made (12 of 12 rows).")
    assert "select 'iri' AS made_kind with made" in handle["note"]
    receipt, _ = run("receipt", "made", workspace=ws)
    assert receipt["detail"]["plain_text"] == {"made": 12}

    # stated: the caller says it is an IRI, and it is one
    stated = built.replace(" AS made FROM", " AS made, 'iri' AS made_kind FROM")
    handle, _ = run("sql", stated, "--out", "iris", workspace=ws)
    assert handle["columns"] == ["made"] and "note" not in handle
    assert {(r["made_kind"], r["made_lang"]) for r in rows(ws, "iris")} == {("iri", None)}
    _, err = run("sql", stated.replace("'iri'", "'IRI'"), workspace=ws, expect=2)
    assert "made_kind is 'IRI'; a kind is one of iri, literal, bnode" in err


def test_a_derived_table_is_graded_by_the_weakest_table_it_read(ws):
    """One hop (D17): a table that read something imported is `external`,
    whatever else it read. One that read nothing holds what its caller typed."""
    workspace = Workspace(ws)
    typed = pa.table(
        {
            c: pa.array(v, type=pa.string())
            for c, v in {
                "gene": [EX + "gene/G2"],
                "gene_kind": ["iri"],
                "gene_lang": [None],
                "gene_datatype": [None],
            }.items()
        }
    )
    receipt = new_receipt(
        kind="import",
        step=None,
        grade="external",
        sources=[],
        shape="bindings",
        completeness={"complete": True, "incomplete_because": []},
        totals={"requests": 0},
    )
    workspace.write_table("mine", typed, receipt)
    handle, _ = run(
        "sql",
        "SELECT a.chem FROM affects a JOIN mine m ON a.gene = m.gene",
        "--out",
        "j",
        workspace=ws,
    )
    assert handle["grade"] == "external" and handle["reads"] == ["affects", "mine"]
    log, _ = run("log", workspace=ws)
    assert log["steps"][-1]["outputs"][0]["grade"] == "external"
    assert log["steps"][-1]["inputs"]["mine"]["grade"] == "external"
    handle, _ = run("sql", "SELECT 1 AS one, 'x' AS s", "--out", "typed", workspace=ws)
    assert handle["grade"] == "asserted" and handle["reads"] == []
    assert handle["preview"] == [{"one": 1, "s": "x"}]


@pytest.mark.parametrize(
    ("statement", "said"),
    [
        ("SELECT * FROM read_csv('/etc/hosts')", "file system operations are disabled"),
        # a path is not a table: with no access to files it is a name nothing has
        ("SELECT * FROM '/etc/hosts'", "Table with name /etc/hosts does not exist"),
        (
            "SELECT * FROM read_csv('https://example.org/x.csv')",
            "file system operations are disabled",
        ),
        ("SELECT * FROM glob('/*')", "file system operations are disabled"),
        (
            "SELECT * FROM read_parquet('tables/affects.parquet')",
            "file system operations are disabled",
        ),
        ("COPY affects TO 'out.csv'", "Only a single SELECT is run here"),
        ("COPY (SELECT * FROM affects) TO 'out.csv'", "this is COPY"),
        ("ATTACH 'other.db'", "this is ATTACH"),
        ("CREATE TABLE t AS SELECT * FROM affects", "this is CREATE"),
        ("DROP TABLE affects", "this is DROP"),
        ("SET enable_external_access = true", "this is SET"),
        ("INSTALL httpfs", "Only a single SELECT is run here"),
        ("LOAD httpfs", "Only a single SELECT is run here"),
        ("SELECT 1; SELECT 2", "this is SELECT, SELECT"),
        ("SELECT * FROM affects; DROP TABLE affects", "this is SELECT, DROP"),
        ("", "sql needs a SELECT statement"),
        ("SELEC 1", "Only a single SELECT is run here. DuckDB says:"),
        ("SELECT * FROM nowhere", "Table with name nowhere does not exist"),
        ("SELECT chem, chem FROM affects", "more than one column called chem"),
    ],
)
def test_the_sandbox_refuses_everything_but_a_select_over_the_workspace(ws, statement, said):
    """D6 as behaviour. The refusals are DuckDB's or the parser's account of what
    it parsed, never a pattern matched against the statement's text."""
    _, err = run("sql", statement, workspace=ws, expect=2)
    assert said in err, err
    log, _ = run("log", workspace=ws)
    assert "sql" not in {r["operation"] for r in log["steps"]}
    assert sorted(p.stem for p in (ws / "tables").glob("*.parquet")) == [
        *("affects", "mass", "names", "pathways"),
    ]
    assert not (ws / "out.csv").exists() and not (ws / "other.db").exists()


def test_the_sandbox_is_locked_and_sees_only_the_tables_named(ws):
    workspace = Workspace(ws)
    secret = pa.table({"leak": ["a Python variable is not a table"]})  # noqa: F841 — must not be readable by name
    with pytest.raises(sandbox.SqlRefused, match="Table with name secret does not exist"):
        sandbox.select(workspace, "SELECT * FROM secret")
    settings = sandbox.select(
        workspace,
        "SELECT current_setting('enable_external_access') AS ext, "
        "current_setting('lock_configuration') AS locked, "
        "current_setting('autoload_known_extensions') AS autoload, "
        "current_setting('python_enable_replacements') AS py",
    ).table.to_pylist()
    assert settings == [{"ext": False, "locked": True, "autoload": False, "py": False}]
    # only what the statement names is registered, and CTEs are not tables of ours
    seen = sandbox.select(
        workspace,
        "WITH g AS (SELECT gene FROM affects) SELECT view_name FROM duckdb_views() "
        "WHERE NOT internal ORDER BY 1",
    )
    assert seen.reads == ["affects"] and seen.table.column("view_name").to_pylist() == ["affects"]
    # a join by USING, a subquery and a CTE are each read for their tables
    for statement, reads in (
        ("SELECT * FROM affects JOIN pathways USING (gene)", ["affects", "pathways"]),
        (
            "SELECT chem FROM affects WHERE gene IN (SELECT gene FROM PATHWAYS)",
            ["affects", "pathways"],
        ),
        ("WITH m AS (SELECT * FROM mass) SELECT count(*) FROM m", ["mass"]),
        ("SELECT 1", []),
    ):
        assert sandbox.select(workspace, statement).reads == reads
    # spills go to the workspace's own scratch directory
    where = sandbox.select(workspace, "SELECT current_setting('temp_directory') AS t")
    assert where.table.to_pylist() == [{"t": str(workspace.scratch_dir() / "duckdb")}]


def test_no_file_a_statement_names_is_opened_at_any_point(ws, tmp_path):
    """Finding which tables a statement reads must not be a way around the
    lock. The first version asked DuckDB's `get_table_names` on a connection
    that was not locked, and that binds as it goes: it opened the file a table
    function named before the sandbox had seen the statement. A named pipe
    shows it: opening one for reading waits for a writer, and DuckDB waits
    holding the interpreter, so the old way hung its whole process. Hence a
    subprocess with a time limit: nothing may so much as open the file."""
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    script = (
        "import json, sys\n"
        "from kgfq.workspace import sandbox\n"
        "from kgfq.workspace.store import Workspace\n"
        "said = []\n"
        "for function in ('read_csv', 'read_parquet', 'read_json', 'read_text'):\n"
        "    try:\n"
        "        sandbox.select(Workspace(sys.argv[1]), f\"SELECT * FROM {function}('{sys.argv[2]}')\")\n"
        "    except sandbox.SqlRefused as e:\n"
        "        said.append(str(e))\n"
        "print(json.dumps(said))\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", script, str(ws), str(pipe)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("something opened the file the statement named, and is waiting on it")
    assert done.returncode == 0, done.stderr
    refused = json.loads(done.stdout)
    assert len(refused) == 4
    assert all("file system operations are disabled" in said for said in refused), refused


def test_a_value_that_looks_like_sql_is_a_value(ws):
    """A literal can hold anything, a statement fragment included. It is a cell,
    compared and returned as text; nothing here builds SQL from a value."""
    workspace = Workspace(ws)
    hostile = "x'; DROP TABLE affects; SELECT '"
    table = pa.table(
        {
            c: pa.array(v, type=pa.string())
            for c, v in {
                "label": [hostile, "ordinary"],
                "label_kind": ["literal"] * 2,
                "label_lang": [None] * 2,
                "label_datatype": [None] * 2,
            }.items()
        }
    )
    receipt = new_receipt(
        kind="import",
        step=None,
        grade="external",
        sources=[],
        shape="bindings",
        completeness={"complete": True, "incomplete_because": []},
        totals={"requests": 0},
    )
    workspace.write_table("labels_in", table, receipt)
    handle, _ = run(
        "sql",
        "SELECT label FROM labels_in WHERE label LIKE '%DROP%'",
        "--out",
        "found",
        workspace=ws,
    )
    assert [r["label"] for r in rows(ws, "found")] == [hostile] and handle["rows"] == 1
    assert (ws / "tables" / "affects.parquet").exists()
    after, _ = run("sql", "SELECT count(*) AS n FROM affects", "--out", "still", workspace=ws)
    assert rows(ws, "still")[0]["n"] == "24" and after["grade"] == "bundle"


def test_sql_is_bounded_in_rows_and_in_time(ws):
    handle, _ = run(
        "sql", "SELECT * FROM affects", "--max-rows", "5", "--out", "five", workspace=ws
    )
    assert handle["rows"] == 5 and handle["complete"] is False
    assert handle["approximations"] == [
        "stopped at the row budget of 5: which rows, not how many, is unspecified"
    ]
    receipt, _ = run("receipt", "five", workspace=ws)
    assert receipt["core"]["completeness"]["incomplete_because"] == ["max_rows"]
    assert receipt["detail"]["budget"] == {"max_rows": 5, "timeout_s": 60.0}
    log, _ = run("log", workspace=ws)
    assert log["steps"][-1]["args"]["max_rows"] == 5
    # a join with no condition, stopped; nothing is filed
    runaway = (
        "SELECT count(*) AS n FROM range(100000000) a, range(100000000) b WHERE a.range <> b.range"
    )
    _, err = run("sql", runaway, "--timeout", "0.5", "--out", "never", workspace=ws, expect=2)
    assert "stopped at the time budget of 0.5 s" in err
    assert not (ws / "tables" / "never.parquet").exists()


def test_js_number_spells_a_double_as_javascript_does():
    for value, text in (
        (1.5, "1.5"),
        (100.0, "100"),
        (0.1 + 0.2, "0.30000000000000004"),
        (1e21, "1e+21"),
        (1e16, "10000000000000000"),
        (1.2345678901234568e20, "123456789012345680000"),
        (1e-7, "1e-7"),
        (0.000001, "0.000001"),
        (-2.5e-10, "-2.5e-10"),
        (123456.789, "123456.789"),
        (0.0, "0"),
        (-0.0, "0"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
        (float("nan"), "NaN"),
    ):
        assert sandbox.js_number(value) == text, value


def test_a_computed_value_is_a_literal_of_its_type():
    def lit(value, kind):
        got = sandbox.computed_literal(value, kind)
        assert got is not None
        return got.value, got.datatype

    assert sandbox.computed_literal(None, pa.int64()) is None
    assert lit(7, pa.int32()) == ("7", XSD + "integer")
    assert lit(decimal.Decimal("12.50"), pa.decimal128(9, 2)) == ("12.50", XSD + "decimal")
    assert lit(decimal.Decimal("1E+3"), pa.decimal128(38, 0)) == ("1000", XSD + "decimal")
    assert lit(datetime.date(2026, 11, 23), pa.date32()) == ("2026-11-23", XSD + "date")
    assert lit(datetime.datetime(2026, 11, 23, 9, 30), pa.timestamp("us")) == (
        "2026-11-23T09:30:00",
        XSD + "dateTime",
    )
    assert lit(True, pa.bool_()) == ("true", XSD + "boolean")
    assert lit(2.5, pa.float64()) == ("2.5", XSD + "double")
    assert lit([1, "a"], pa.list_(pa.string())) == ('[1, "a"]', None)
    assert lit("plain", pa.string()) == ("plain", None)


def test_a_preview_shows_a_number_as_a_number_where_json_holds_it_exactly():
    assert compact(Literal("3762", datatype=XSD + "integer")) == 3762
    assert compact(Literal("107.5", datatype=XSD + "double")) == 107.5
    assert compact(Literal("12.50", datatype=XSD + "decimal")) == 12.5
    assert compact(Literal("true", datatype=XSD + "boolean")) is True
    # past what a JSON number holds exactly, the text is the value
    big = Literal("9007199254740993", datatype=XSD + "integer")
    assert compact(big) == f'"9007199254740993"^^<{XSD}integer>'
    assert compact(Literal("NaN", datatype=XSD + "double")) == f'"NaN"^^<{XSD}double>'
    assert compact(Literal("twelve", datatype=XSD + "integer")) == f'"twelve"^^<{XSD}integer>'
    assert compact(Literal("chat", lang="fr")) == '"chat"@fr'
    assert compact(Literal("2026-11-23", datatype=XSD + "date")) == f'"2026-11-23"^^<{XSD}date>'
    assert compact(Iri("http://x/a")) == "http://x/a" and compact(None) is None
