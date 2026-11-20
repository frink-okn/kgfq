import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kgfq import __version__
from kgfq.workspace.log import Step, classify_file, weakest_grade
from kgfq.workspace.receipts import new_receipt
from kgfq.workspace.store import Pin, TableHash, Workspace, WorkspaceError


def triples(rows):
    s, p, o = zip(*rows, strict=True)
    return pa.table(
        {
            "s": list(s),
            "p": list(p),
            "o": list(o),
            "o_kind": ["iri"] * len(rows),
            "o_lang": [None] * len(rows),
            "o_datatype": [None] * len(rows),
        }
    )


def receipt(step=1, grade="bundle"):
    return new_receipt(
        kind="fetch",
        step=step,
        grade=grade,
        sources=[],
        shape="triples",
        completeness={"complete": True, "incomplete_because": []},
        totals={"requests": 0},
    )


def test_table_write_carries_receipt_in_footer_and_sidecar(tmp_path):
    ws = Workspace(tmp_path)
    t = triples([("a", "p", "x"), ("b", "p", "y")])
    given = receipt()
    info = ws.write_table("t1", t, given)
    assert info.rows == 2 and info.columns[:3] == ["s", "p", "o"]
    sidecar = json.loads(ws.receipt_path("t1").read_text())
    footer = json.loads(pq.read_schema(ws.table_path("t1")).metadata[b"kgf.receipt"])
    assert sidecar == footer == info.receipt
    core = footer["core"]
    assert core["content_hash"] == info.content_hash == Workspace.content_hash(t)
    assert (core["table"], core["rows"], core["columns"]) == ("t1", 2, t.column_names)
    assert "table" not in given["core"], "the receipt passed in is not written to"
    assert ws.list_tables() == ["t1"]
    assert ws.read_receipt("t1")["core"]["grade"] == "bundle"


def test_a_tables_receipt_is_its_footer_and_a_sidecar_stands_alone_only_without_one(tmp_path):
    """The footer cannot be parted from the rows, so it is the receipt of record.
    A session another client wrote need not have sidecars; a step that wrote no
    table has only one."""
    ws = Workspace(tmp_path)
    ws.write_table("t1", triples([("a", "p", "x")]), receipt())
    ws.receipt_path("t1").write_text(json.dumps({"core": {"grade": "asserted"}}))
    assert ws.read_receipt("t1")["core"]["grade"] == "bundle"
    ws.receipt_path("t1").unlink()
    assert ws.read_receipt("t1")["core"]["table"] == "t1"
    ws.write_receipt("aborted", receipt(step=2))
    assert ws.read_receipt("aborted")["core"]["step"] == 2 and not ws.has_table("aborted")
    with pytest.raises(WorkspaceError, match="no table or receipt named 'nothing'"):
        ws.read_receipt("nothing")
    pq.write_table(triples([("a", "p", "x")]), ws.table_path("bare"))
    with pytest.raises(WorkspaceError, match="carries no receipt in its footer"):
        ws.read_receipt("bare")


def test_content_hash_ignores_row_order(tmp_path):
    a = triples([("a", "p", "x"), ("b", "p", "y")])
    b = triples([("b", "p", "y"), ("a", "p", "x")])
    c = triples([("b", "p", "y"), ("a", "p", "z")])
    assert Workspace.content_hash(a) == Workspace.content_hash(b) != Workspace.content_hash(c)


def test_content_hash_is_the_specified_bytes():
    """The hash worked out by hand from its definition, so a second client has
    a vector to reproduce: each row's cells as bytes, the rows' SHA-256 digests
    added modulo 2**256, and one SHA-256 over the preamble, the column names,
    the row count and the sum."""
    table = pa.table(
        {
            "x": pa.array(["\U00010000", "\uffff", None], type=pa.string()),
            "n": pa.array([2, 1, None], type=pa.int64()),
            "ok": pa.array([True, False, None], type=pa.bool_()),
        }
    )
    rows = [b"S4:\xf0\x90\x80\x80I2;B1", b"S3:\xef\xbf\xbfI1;B0", b"NNN"]
    total = sum(int.from_bytes(hashlib.sha256(r).digest(), "big") for r in rows) % 2**256
    expected = b"kgf.table-hash/1\nS1:xS1:nS2:okR3;" + total.to_bytes(32, "big")
    assert Workspace.content_hash(table) == "sha256:" + hashlib.sha256(expected).hexdigest()
    # the same, as the literal another client's test can hold
    assert Workspace.content_hash(table) == (
        "sha256:7707f027675ea8c04db9e1538823c3dd4dd61738e531cb742fbf0992ca6cc96d"
    )


def test_content_hash_is_taken_a_row_at_a_time_in_any_order():
    """Nothing is sorted and nothing is held: rows fed in pieces, in any order,
    give the hash of the table whole, so a fetch can hash its pages as they
    arrive. A repeated row counts each time, since a table is a bag of rows."""
    rows = [("a", "p", "x"), ("b", "p", "y"), ("b", "p", "z"), ("c", "q", None)]
    whole = TableHash(["s", "p", "o"])
    whole.update(rows)
    pieces = TableHash(["s", "p", "o"])
    pieces.update(rows[2:])
    pieces.update([])
    pieces.update(reversed(rows[:2]))
    assert pieces.hexdigest() == whole.hexdigest() and pieces.rows == 4
    columns = zip("spo", zip(*rows, strict=True), strict=True)
    table = pa.table({c: pa.array(v, type=pa.string()) for c, v in columns})
    assert Workspace.content_hash(table) == whole.hexdigest()
    chunked = pa.concat_tables([table.slice(0, 1), table.slice(1)])
    assert chunked.column("s").num_chunks == 2
    assert Workspace.content_hash(chunked) == whole.hexdigest()
    twice = TableHash(["s", "p", "o"])
    twice.update([*rows, rows[0]])
    assert twice.hexdigest() != whole.hexdigest()
    empty = TableHash(["s", "p", "o"])
    assert empty.hexdigest() != TableHash(["s", "p"]).hexdigest()


def test_content_hash_tells_cells_columns_and_types_apart():
    # No escaping is needed: a cell cannot be mistaken for two.
    one = pa.table({"a": ["S1:x"], "b": ["y"]})
    two = pa.table({"a": ["S1:"], "b": ["xy"]})
    assert Workspace.content_hash(one) != Workspace.content_hash(two)
    # A column's name and its type are part of the table.
    assert Workspace.content_hash(pa.table({"a": ["1"]})) != Workspace.content_hash(
        pa.table({"a": [1]})
    )
    assert Workspace.content_hash(pa.table({"a": ["1"]})) != Workspace.content_hash(
        pa.table({"b": ["1"]})
    )
    with pytest.raises(WorkspaceError, match="float cell has no specified hash encoding"):
        Workspace.content_hash(pa.table({"a": [1.5]}))


def test_endpoints_and_pins(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.endpoints() == []
    ws.add_endpoint("http://a/")
    ws.add_endpoint("http://b")
    ws.add_endpoint("http://a")
    assert ws.endpoints() == ["http://a", "http://b"]
    ws.remove_endpoint("http://a")
    assert ws.endpoints() == ["http://b"]
    ws.pin("x", Pin("http://b", "x", "1", "sha256:0", None))
    assert ws.pins()["x"].version == "1"
    ws.unpin("x")
    assert ws.pins() == {}


def test_log_steps_and_classification(tmp_path):
    ws = Workspace(tmp_path)
    step = Step(ws, "cli", "fetch", {"graph": "chemkg"})
    assert step.step == 1
    info = ws.write_table("genes", triples([("http://g/1", "p", "http://x/1")]), receipt())
    step.table(info.receipt)
    rec = step.finish()
    assert rec["step"] == 1 and rec["status"] == "ok" and rec["schema"] == "kgf.session/1.0"
    assert rec["client"] == {"name": "kgfq", "version": __version__, "surface": "cli"}
    assert rec["actor"] is None and rec["operation"] == "fetch"
    assert rec["outputs"] == [
        {
            "table": "genes",
            "shape": "triples",
            "rows": 1,
            "grade": "bundle",
            "content_hash": info.content_hash,
        }
    ]
    assert ws.read_log() == [rec]

    step2 = Step(ws, "mcp", "count", {}, actor="assistant")
    assert step2.record["actor"] == "assistant" and step2.record["client"]["surface"] == "mcp"
    assert step2.step == 2
    got = step2.input_values(["http://g/1", "http://x/1", "http://nowhere"])
    assert got["http://g/1"] == {"kind": "derived", "table": "genes", "step": 1, "grade": "bundle"}
    assert got["http://x/1"]["kind"] == "derived"
    assert got["http://nowhere"] == {"kind": "asserted"}
    other = tmp_path / "ids.txt"
    other.write_text("x\n")
    assert classify_file(ws, other)["kind"] == "external"
    assert classify_file(ws, ws.table_path("genes"))["kind"] == "derived"
    step2.finish()
    assert [r["step"] for r in ws.read_log()] == [1, 2]


def test_weakest_grade():
    assert weakest_grade(["bundle", "bundle"]) == "bundle"
    assert weakest_grade(["bundle", "sparql"]) == "sparql"
    assert weakest_grade(["external", "sparql"]) == "external"
    assert weakest_grade(["bundle", "asserted"]) == "asserted"
    assert weakest_grade([]) == "bundle"
