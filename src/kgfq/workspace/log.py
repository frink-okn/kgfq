"""Log records (C9) and the derived-versus-asserted classification (D17).

Every library operation is one record. Each input the step consumed is looked
up among the outputs of earlier steps — workspace tables, and files the session
wrote — and classified *derived* (with the step that produced it) or
*asserted* (typed by the agent or the user). The classification is set
membership over logged outputs; it needs nothing from the model.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa

from ..client.terms import term_column_names
from .store import Workspace

LOG_SCHEMA = "kgfq.log/1"
GRADES = ("bundle", "sparql", "external", "asserted")


def classify_values(ws: Workspace, values: Iterable[str]) -> dict[str, dict[str, Any]]:
    """For each value, the earliest workspace table (and its step) with a term
    column that carries it, else ``asserted``. Every term column is read: a
    fetched table's are its pattern's variables (C6, 2026-11-20), whatever the
    caller called them."""
    wanted = {v for v in values if v}
    out: dict[str, dict[str, Any]] = {v: {"kind": "asserted"} for v in wanted}
    if not wanted:
        return out
    remaining = set(wanted)
    for name in ws.list_tables():
        if not remaining:
            break
        try:
            table = ws.read_table(name)
        except Exception:
            continue
        cols = [
            c
            for c in term_column_names(table.column_names)
            if pa.types.is_string(table.schema.field(c).type)
        ]
        if not cols:
            continue
        core = ws.read_receipt(name)["core"]
        for c in cols:
            present = set(table.column(c).unique().to_pylist()) & remaining
            for v in present:
                out[v] = {
                    "kind": "derived",
                    "table": name,
                    "step": core.get("step"),
                    "grade": core.get("grade"),
                }
            remaining -= present
    return out


def classify_file(ws: Workspace, path: Path) -> dict[str, Any]:
    """A file input: a workspace table is derived from the step that wrote it;
    anything else is external, identified by content hash."""
    path = Path(path)
    if path.suffix == ".parquet" and path.resolve().parent == ws.tables_dir.resolve():
        core = ws.read_receipt(path.stem)["core"]
        return {
            "kind": "derived",
            "table": path.stem,
            "step": core.get("step"),
            "grade": core.get("grade"),
        }
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"kind": "external", "path": str(path), "sha256": h.hexdigest()}


class Step:
    """Builds one log record and appends it when finished."""

    def __init__(self, ws: Workspace, skin: str, command: str, args: Mapping[str, Any]) -> None:
        self.ws = ws
        self.record: dict[str, Any] = {
            "schema": LOG_SCHEMA,
            "step": ws.next_step(),
            "skin": skin,
            "command": command,
            "args": dict(args),
            "inputs": {},
            "pins": [],
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._t0 = time.perf_counter()

    @property
    def step(self) -> int:
        return int(self.record["step"])

    def input_values(self, values: Iterable[str]) -> dict[str, dict[str, Any]]:
        classified = classify_values(self.ws, values)
        self.record["inputs"].update(classified)
        return classified

    def input_file(self, path: Path) -> dict[str, Any]:
        info = classify_file(self.ws, path)
        self.record["inputs"][str(path)] = info
        return info

    def pin(self, pin: Mapping[str, Any]) -> None:
        self.record["pins"].append(dict(pin))

    def plan(self, plan: Mapping[str, Any]) -> None:
        self.record["plan"] = dict(plan)

    def receipt(self, receipt: Mapping[str, Any]) -> None:
        self.record["receipt"] = dict(receipt)

    def output(self, **fields: Any) -> None:
        self.record["output"] = dict(fields)

    def finish(self, status: str = "ok", error: str | None = None) -> dict[str, Any]:
        self.record["status"] = status
        if error:
            self.record["error"] = error
        self.record["elapsed_ms"] = round((time.perf_counter() - self._t0) * 1000, 3)
        return self.ws.append_log(self.record)


def weakest_grade(grades: Iterable[str]) -> str:
    """A derived table's grade is the minimum over its inputs (D17)."""
    order = {g: i for i, g in enumerate(GRADES)}
    worst = "bundle"
    for g in grades:
        if order.get(g, len(GRADES)) > order[worst]:
            worst = g if g in order else "asserted"
    return worst
