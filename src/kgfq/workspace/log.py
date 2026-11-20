"""Log records (C9) and the derived-versus-asserted classification (D17).

Every library operation is one record, in the envelope kgfq shares with
``../kgf-explorer`` (``notes/session-format.md`` §3): what every reader
understands around what only the operation's implementers do. Each input the step consumed is looked
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

from .. import __version__
from ..client.terms import term_column_names
from .store import Workspace

LOG_SCHEMA = "kgf.session/1.0"
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
    # In step order, so a value is derived from where it first appeared: the
    # listing is by name, and `affects` at step 4 would answer for `chems` at 3.
    cores: dict[str, dict[str, Any]] = {}
    for name in ws.list_tables():
        try:
            cores[name] = ws.read_receipt(name)["core"]
        except Exception:
            continue
    by_step = sorted(
        cores, key=lambda n: (cores[n].get("step") is None, cores[n].get("step") or 0, n)
    )
    for name in by_step:
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
        core = cores[name]
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
    """Builds one log record, a ``kgf.session`` envelope
    (``docs/session/envelope.schema.json``), and appends it when finished.

    ``operation`` is a registry name and ``args`` its arguments in the
    registry's spelling (``docs/session/operations.yaml``), whatever the surface
    took. ``actor`` is who asked, ``user`` or ``assistant``, and ``None`` where
    the surface cannot tell: a command line is typed by a person or by an agent
    alike."""

    def __init__(
        self,
        ws: Workspace,
        surface: str,
        operation: str,
        args: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> None:
        self.ws = ws
        self.record: dict[str, Any] = {
            "schema": LOG_SCHEMA,
            "step": ws.next_step(),
            "client": {"name": "kgfq", "version": __version__, "surface": surface},
            "actor": actor,
            "operation": operation,
            "args": dict(args),
            "inputs": {},
            "pins": [],
            "outputs": [],
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

    def receipt(self, receipt: Mapping[str, Any]) -> None:
        """The step's receipt, whole: the one a table carries in its footer, or
        the only copy when the step wrote no table."""
        self.record["receipt"] = dict(receipt)

    def table(self, filed: Mapping[str, Any], *, path: str | None = None) -> None:
        """A table the step wrote, from the core of its receipt as filed.
        ``path`` is said only for a table written outside the workspace."""
        core = filed["core"]
        entry = {k: core.get(k) for k in ("table", "shape", "rows", "grade", "content_hash")}
        if path is not None:
            entry["path"] = path
        self.record["outputs"].append(entry)

    def result(self, **fields: Any) -> None:
        """What the step answered without writing a table: a count, an ASK."""
        self.record["result"] = dict(fields)

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
