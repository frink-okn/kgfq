"""The workspace (plan C9, D15): the directory an analysis leaves behind.

```
<workspace>/
  .kgfq/              session state: endpoints, pins, caches, scratch — disposable
  kgfq.log.jsonl      one record per library operation, append-only
  tables/*.parquet    evidence; each with a *.receipt.json sidecar and the same
                      receipt in the Parquet footer under ``kgfq.receipt``
```

Files rather than a database: no writer lock between parallel invocations, a
receipt that travels inside the file, and ``ls`` as the audit tool.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

RECEIPT_KEY = b"kgfq.receipt"
TABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Pin:
    origin: str
    dataset: str
    version: str
    content_digest: str
    dataset_iri: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "dataset": self.dataset,
            "version": self.version,
            "content_digest": self.content_digest,
            "dataset_iri": self.dataset_iri,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Pin:
        return cls(
            d["origin"], d["dataset"], d["version"], d["content_digest"], d.get("dataset_iri")
        )


@dataclass(frozen=True, slots=True)
class TableInfo:
    name: str
    path: Path
    rows: int
    columns: list[str]
    content_hash: str
    receipt: dict[str, Any]


class Workspace:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.environ.get("KGFQ_WORKSPACE") or os.getcwd()).resolve()
        self.state = self.path / ".kgfq"
        self.tables_dir = self.path / "tables"
        self.log_path = self.path / "kgfq.log.jsonl"

    # -- state files --------------------------------------------------------

    def _read_json(self, name: str, default: Any) -> Any:
        p = self.state / name
        if not p.exists():
            return default
        return json.loads(p.read_text())

    def _write_json(self, name: str, value: Any) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.state / f".{name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, self.state / name)

    def endpoints(self) -> list[str]:
        return list(self._read_json("endpoints.json", []))

    def add_endpoint(self, origin: str) -> list[str]:
        origin = origin.rstrip("/")
        current = self.endpoints()
        if origin not in current:
            current.append(origin)
            self._write_json("endpoints.json", current)
        return current

    def remove_endpoint(self, origin: str) -> list[str]:
        origin = origin.rstrip("/")
        current = [o for o in self.endpoints() if o != origin]
        self._write_json("endpoints.json", current)
        return current

    def pins(self) -> dict[str, Pin]:
        return {k: Pin.from_dict(v) for k, v in self._read_json("pins.json", {}).items()}

    def pin(self, key: str, pin: Pin) -> None:
        current = self._read_json("pins.json", {})
        current[key] = pin.as_dict()
        self._write_json("pins.json", current)

    def unpin(self, key: str | None = None) -> None:
        if key is None:
            self._write_json("pins.json", {})
            return
        current = self._read_json("pins.json", {})
        current.pop(key, None)
        self._write_json("pins.json", current)

    def cache_dir(self, kind: str) -> Path:
        d = self.state / "cache" / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    def clear_cache(self, kind: str) -> None:
        shutil.rmtree(self.state / "cache" / kind, ignore_errors=True)

    def scratch_dir(self) -> Path:
        d = self.state / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- tables -------------------------------------------------------------

    @staticmethod
    def check_name(name: str) -> str:
        if not TABLE_NAME.fullmatch(name):
            raise WorkspaceError(f"table name {name!r} must match {TABLE_NAME.pattern}")
        return name

    def table_path(self, name: str) -> Path:
        return self.tables_dir / f"{self.check_name(name)}.parquet"

    def receipt_path(self, name: str) -> Path:
        return self.tables_dir / f"{self.check_name(name)}.receipt.json"

    def engine_receipt_path(self, name: str) -> Path:
        """Where ``kgfq sparql`` files the receipt ``kgf-sparql`` wrote, whole,
        beside the table it describes (C6)."""
        return self.tables_dir / f"{self.check_name(name)}.kgf-sparql.receipt.json"

    def relative(self, path: Path) -> str:
        """A path as the receipt records it: relative to the workspace when inside it."""
        try:
            return str(Path(path).resolve().relative_to(self.path))
        except ValueError:
            return str(path)

    def has_table(self, name: str) -> bool:
        return self.table_path(name).exists()

    def list_tables(self) -> list[str]:
        if not self.tables_dir.exists():
            return []
        return sorted(p.stem for p in self.tables_dir.glob("*.parquet"))

    def write_table(self, name: str, table: pa.Table, receipt: dict[str, Any]) -> TableInfo:
        """Write ``tables/<name>.parquet`` with the receipt in the footer and a
        sidecar; the receipt gains the table's rows, columns and content hash."""
        self.check_name(name)
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        content_hash = self.content_hash(table)
        receipt = dict(receipt)
        receipt.update(
            {
                "table": name,
                "rows": table.num_rows,
                "columns": table.column_names,
                "content_hash": content_hash,
            }
        )
        payload = json.dumps(receipt, sort_keys=True).encode()
        meta = dict(table.schema.metadata or {})
        meta[RECEIPT_KEY] = payload
        table = table.replace_schema_metadata(meta)
        path = self.table_path(name)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, path)
        self.write_receipt(name, receipt)
        return TableInfo(name, path, table.num_rows, table.column_names, content_hash, receipt)

    def write_receipt(self, name: str, receipt: dict[str, Any]) -> Path:
        """The sidecar alone: for a table, and for a step that produced none —
        a run aborted at a budget is a finding with a receipt (D2) and no table."""
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        path = self.receipt_path(name)
        path.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
        return path

    def remove_table(self, name: str) -> bool:
        """Drop ``tables/<name>.parquet`` so a name never describes a table its
        sidecar does not; true when there was one."""
        path = self.table_path(name)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def read_table(self, name: str, columns: list[str] | None = None) -> pa.Table:
        return pq.read_table(self.table_path(name), columns=columns)

    def read_receipt(self, name: str) -> dict[str, Any]:
        sidecar = self.receipt_path(name)
        if sidecar.exists():
            return json.loads(sidecar.read_text())
        meta = pq.read_schema(self.table_path(name)).metadata or {}
        if RECEIPT_KEY in meta:
            return json.loads(meta[RECEIPT_KEY])
        raise WorkspaceError(f"table {name!r} carries no receipt")

    @staticmethod
    def content_hash(table: pa.Table) -> str:
        """A deterministic hash of the rows regardless of their order: the
        canonical (sorted) row serialization, so replay (C10) compares equal
        results that arrived in a different order."""
        columns = table.column_names
        rows = (
            [
                json.dumps([_plain(v) for v in row], separators=(",", ":"), ensure_ascii=False)
                for row in zip(*(table.column(c).to_pylist() for c in columns), strict=True)
            ]
            if table.num_rows
            else []
        )
        rows.sort()
        h = hashlib.sha256()
        h.update(json.dumps(columns, separators=(",", ":")).encode())
        for r in rows:
            h.update(b"\n")
            h.update(r.encode())
        return "sha256:" + h.hexdigest()

    # -- the log ------------------------------------------------------------

    def read_log(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]

    def iter_log(self) -> Iterator[dict[str, Any]]:
        yield from self.read_log()

    def next_step(self) -> int:
        records = self.read_log()
        return max((r.get("step", 0) for r in records), default=0) + 1

    def append_log(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record)
        record.setdefault("step", self.next_step())
        record.setdefault("time", _now())
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return record


def _plain(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
