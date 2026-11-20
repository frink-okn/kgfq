"""The session kgfq keeps as the format's fixture (``notes/session-format.md``
§9): ``docs/session/fixtures/kgfq``. A second client's tests load it, so two
things are held here. It reads as any session does, with no server: every
record and receipt validates, every table hashes to what its record says, and
every fetched table exports the triples filed beside it. And it is still what
kgfq writes: the same commands against the fixture servers give the same
steps, tables and hashes.

``KGFQ_WRITE_FIXTURE=1 uv run pytest tests/test_session_fixture.py`` rewrites
it, when the format changes on purpose.
"""

import io
import json
import os
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml

from kgfq.cli import main
from kgfq.workspace.store import Workspace
from kgfq.workspace.triples import triples, write_ntriples

SESSION = Path(__file__).parent.parent / "docs" / "session"
FIXTURE = SESSION / "fixtures" / "kgfq"
RECEIPT = jsonschema.Draft202012Validator(json.loads((SESSION / "receipt.schema.json").read_text()))
ENVELOPE = jsonschema.Draft202012Validator(
    json.loads((SESSION / "envelope.schema.json").read_text())
)
OPERATIONS = yaml.safe_load((SESSION / "operations.yaml").read_text())["operations"]

CHEMS = "?chem\n<http://example.org/chem/1>\n<http://example.org/chem/4>\n<http://example.org/chem/99>\n"
# One of each thing a reader meets: a step with no table, language-tagged and
# typed literals, a bound fetch with a value that finds nothing, a second graph
# on a second server, anonymous positions, and a constant an earlier table held.
COMMANDS = [
    ["summary", "chemkg"],
    ["count", "chemkg", "? ex:affects ?"],
    ["fetch", "chemkg", "?chem rdfs:label ?name", "--out", "chems"],
    [
        "fetch",
        "chemkg",
        "?chem ex:affects ?gene",
        "--values",
        "inputs/chems.tsv",
        "--out",
        "affects",
    ],
    ["fetch", "genekg", "?gene ex:inPathway ?pathway", "--out", "pathways"],
    ["fetch", "chemkg", "ex:chem/1 ? ?", "--out", "chem1"],
    ["distinct", "chemkg", "? ex:affects ?gene", "--by", "o", "--top", "3"],
]
FETCHED = ["affects", "chem1", "chems", "pathways"]


def write_session(ws: Path, origins: list[str]) -> None:
    """Run :data:`COMMANDS` in ``ws`` as its working directory, so the paths the
    log records are relative."""
    (ws / "inputs").mkdir()
    (ws / "inputs" / "chems.tsv").write_text(CHEMS)
    here = os.getcwd()
    os.chdir(ws)
    try:
        for argv in [*(["endpoints", "add", o] for o in origins), *COMMANDS]:
            out, err = io.StringIO(), io.StringIO()
            assert main(["--json", *argv], out=out, err=err) == 0, err.getvalue()
    finally:
        os.chdir(here)


def records(ws: Path) -> list[dict]:
    return [json.loads(line) for line in (ws / "kgfq.log.jsonl").read_text().splitlines()]


def export(ws: Workspace, name: str) -> str:
    out = io.StringIO()
    write_ntriples(triples(ws.read_table(name), ws.read_receipt(name)), out)
    return out.getvalue()


def stable(record: dict) -> dict:
    """A record without what differs from one run to the next: times, request
    ids, the fixture servers' ports, and the bundle's digest, which is the build
    toolchain's."""
    core = record["receipt"]["core"]
    return {
        "step": record["step"],
        "client": {k: record["client"][k] for k in ("name", "surface")},
        "actor": record["actor"],
        "operation": record["operation"],
        "args": record["args"],
        "inputs": record["inputs"],
        "pins": [(p["dataset"], p["version"]) for p in record["pins"]],
        "outputs": record["outputs"],
        "result": record.get("result"),
        "status": record["status"],
        "receipt": {
            k: core.get(k)
            for k in (
                *("kind", "grade", "shape", "pattern", "table", "columns", "rows"),
                *("content_hash", "completeness", "approximations"),
            )
        },
    }


def test_the_fixture_is_what_kgfq_still_writes(tmp_path, chem, genes):
    write_session(tmp_path, [chem.origin, genes.origin])
    if os.environ.get("KGFQ_WRITE_FIXTURE"):
        ws = Workspace(tmp_path)
        for part in ("inputs", "tables", "expected"):
            shutil.rmtree(FIXTURE / part, ignore_errors=True)
        (FIXTURE / "expected").mkdir(parents=True)
        shutil.copy(tmp_path / "kgfq.log.jsonl", FIXTURE / "kgfq.log.jsonl")
        shutil.copytree(tmp_path / "inputs", FIXTURE / "inputs")
        shutil.copytree(tmp_path / "tables", FIXTURE / "tables")
        for name in ws.list_tables():
            (FIXTURE / "expected" / f"{name}.nt").write_text(export(ws, name))
        pytest.skip(f"rewrote {FIXTURE}; read the diff before keeping it")
    assert [stable(r) for r in records(tmp_path)] == [stable(r) for r in records(FIXTURE)]
    ws = Workspace(tmp_path)
    for name in FETCHED:
        assert export(ws, name) == (FIXTURE / "expected" / f"{name}.nt").read_text()


def test_the_fixture_reads_as_any_session_does():
    ws = Workspace(FIXTURE)
    log = records(FIXTURE)
    assert [r["operation"] for r in log] == [
        *("graph_summary", "count", "fetch", "fetch", "fetch", "fetch", "distinct"),
    ]
    assert ws.list_tables() == FETCHED
    for r in log:
        ENVELOPE.validate(r)
        RECEIPT.validate(r["receipt"])
        jsonschema.Draft202012Validator(OPERATIONS[r["operation"]]["args"]).validate(r["args"])
    for r in log:
        for wrote in r["outputs"]:
            name = wrote["table"]
            table, filed = ws.read_table(name), ws.read_receipt(name)
            assert filed == r["receipt"], "the footer and the record carry one receipt"
            assert Workspace.content_hash(table) == wrote["content_hash"]
            assert (table.num_rows, table.column_names) == (
                wrote["rows"],
                filed["core"]["columns"],
            )
            assert export(ws, name) == (FIXTURE / "expected" / f"{name}.nt").read_text()
    # what the session shows a reader
    by_table = {o["table"]: r for r in log for o in r["outputs"]}
    assert by_table["affects"]["inputs"]["inputs/chems.tsv"]["kind"] == "external"
    assert by_table["chem1"]["inputs"]["http://example.org/chem/1"] == {
        "kind": "derived",
        "table": "chems",
        "step": 3,
        "grade": "bundle",
    }
    assert by_table["chem1"]["receipt"]["core"]["pattern"] == {
        "s": "<http://example.org/chem/1>",
        "p": "?p",
        "o": "?o",
    }
    origins = {r["pins"][0]["dataset"]: r["pins"][0]["origin"] for r in log}
    assert origins["chemkg"] != origins["genekg"], "two graphs, two servers"
    # chem/99 is asked for and is not in the graph: 2 of 3 values found rows
    affects = ws.read_table("affects")
    assert by_table["affects"]["receipt"]["detail"]["intent"]["bindings"]["distinct"] == 3
    assert len(set(affects["chem"].to_pylist())) == 2
