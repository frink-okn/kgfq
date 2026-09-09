import json

import pyarrow as pa
import pyarrow.parquet as pq

from kgfq.workspace.log import Step, classify_file, weakest_grade
from kgfq.workspace.store import Pin, Workspace


def triples(rows):
    s, p, o = zip(*rows, strict=True)
    return pa.table(
        {
            "s": list(s),
            "p": list(p),
            "o": list(o),
            "o_lang": [None] * len(rows),
            "o_datatype": [None] * len(rows),
        }
    )


def test_table_write_carries_receipt_in_footer_and_sidecar(tmp_path):
    ws = Workspace(tmp_path)
    t = triples([("a", "p", "x"), ("b", "p", "y")])
    info = ws.write_table("t1", t, {"kind": "fetch", "grade": "bundle", "step": 1})
    assert info.rows == 2 and info.columns[:3] == ["s", "p", "o"]
    sidecar = json.loads(ws.receipt_path("t1").read_text())
    footer = json.loads(pq.read_schema(ws.table_path("t1")).metadata[b"kgfq.receipt"])
    assert sidecar == footer == info.receipt
    assert sidecar["content_hash"].startswith("sha256:") and sidecar["table"] == "t1"
    assert ws.list_tables() == ["t1"]
    assert ws.read_receipt("t1")["grade"] == "bundle"


def test_content_hash_ignores_row_order(tmp_path):
    a = triples([("a", "p", "x"), ("b", "p", "y")])
    b = triples([("b", "p", "y"), ("a", "p", "x")])
    c = triples([("b", "p", "y"), ("a", "p", "z")])
    assert Workspace.content_hash(a) == Workspace.content_hash(b) != Workspace.content_hash(c)


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
    step = Step(ws, "cli", "fetch", {"ds": "chemkg"})
    assert step.step == 1
    ws.write_table(
        "genes",
        triples([("http://g/1", "p", "http://x/1")]),
        {"kind": "fetch", "grade": "bundle", "step": 1},
    )
    step.output(table="genes")
    rec = step.finish()
    assert rec["step"] == 1 and rec["status"] == "ok" and rec["schema"] == "kgfq.log/1"
    assert ws.read_log() == [rec]

    step2 = Step(ws, "cli", "count", {})
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
