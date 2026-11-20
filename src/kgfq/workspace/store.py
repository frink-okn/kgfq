"""The workspace (plan C9, D15): the directory an analysis leaves behind.

```
<workspace>/
  .kgfq/              session state: endpoints, pins, caches, scratch — disposable
  kgfq.log.jsonl      one record per library operation, append-only
  tables/*.parquet    evidence; each with its receipt in the Parquet footer under
                      ``kgf.receipt``, and a copy beside it as *.receipt.json
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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

RECEIPT_KEY = b"kgf.receipt"
# The first bytes hashed, so a hash names the encoding it was made with.
HASH_PREAMBLE = b"kgf.table-hash/1\n"
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

    def write_table(self, name: str, table: pa.Table, receipt: Mapping[str, Any]) -> TableInfo:
        """Write ``tables/<name>.parquet`` with the receipt in the footer and a
        sidecar; the receipt's core gains the table's name, rows, columns and
        content hash."""
        self.check_name(name)
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        path = self.table_path(name)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        filed = write_parquet(tmp, table, receipt, name=name)
        os.replace(tmp, path)
        self.write_receipt(name, filed)
        return TableInfo(
            name, path, table.num_rows, table.column_names, filed["core"]["content_hash"], filed
        )

    def write_receipt(self, name: str, receipt: Mapping[str, Any]) -> Path:
        """The sidecar: a copy for ``cat`` and ``jq`` beside a table, whose
        footer is the receipt of record, and the only file a step that produced
        no table leaves — a run aborted at a budget is a finding with a receipt
        (D2)."""
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
        """A table's receipt from its footer, which cannot be parted from the
        rows; the sidecar only where there is no table (2026-11-20). A sidecar
        beside a table is a copy, and a session another client wrote need not
        have one."""
        if self.has_table(name):
            meta = pq.read_schema(self.table_path(name)).metadata or {}
            if RECEIPT_KEY in meta:
                return json.loads(meta[RECEIPT_KEY])
            raise WorkspaceError(f"table {name!r} carries no receipt in its footer")
        sidecar = self.receipt_path(name)
        if sidecar.exists():
            return json.loads(sidecar.read_text())
        raise WorkspaceError(f"no table or receipt named {name!r}")

    @staticmethod
    def content_hash(table: pa.Table) -> str:
        """The table's rows as a hash another client can reproduce, whatever
        order they arrived in, so replay (C10) and a second client compare equal
        results (:class:`TableHash`; ``notes/session-format.md`` §5). Read a
        batch at a time: the table is never held as rows."""
        hasher = TableHash(table.column_names)
        for batch in table.to_batches():
            hasher.update(zip(*(column.to_pylist() for column in batch.columns), strict=True))
        return hasher.hexdigest()

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
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return record


class TableHash:
    """A table's content hash, taken a row at a time in any order and in
    constant memory, so a fetch can hash its pages as they arrive and a table
    larger than memory can still be hashed.

    Each row is its cells in column order, each self-delimiting:

    - null: ``N``
    - string: ``S``, the UTF-8 length in decimal, ``:``, the UTF-8 bytes
    - integer: ``I``, its decimal digits, ``;``
    - boolean: ``B1`` or ``B0``
    - bytes: ``X``, the length in decimal, ``:``, the bytes

    The rows' SHA-256 digests are added as 256-bit big-endian integers, modulo
    2**256. The hash is SHA-256 over :data:`HASH_PREAMBLE`, the column names as
    one row of strings, ``R`` with the row count in decimal and ``;``, and that
    sum as 32 bytes: ``sha256:`` and the hex.

    A sum does not care about order, counts a repeated row each time, and needs
    no sort: the first version sorted every row, which holds the whole table.
    Lengths and a fixed cell count per row need no escaping, which is where
    ``json.dumps`` and ``JSON.stringify`` would have had to agree. A float has
    no spelling two languages share yet and is refused (NaN is null).

    It finds any difference that happens. It is not proof against a table built
    to collide, and need not be: whoever can rewrite a table's rows can rewrite
    the receipt in its footer."""

    def __init__(self, columns: Sequence[str]) -> None:
        self._header = b"".join(map(_cell_bytes, columns))
        self._sum = 0
        self.rows = 0

    def update(self, rows: Iterable[Sequence[Any]]) -> None:
        for row in rows:
            digest = hashlib.sha256(b"".join(map(_cell_bytes, row))).digest()
            self._sum = (self._sum + int.from_bytes(digest, "big")) & _MASK_256
            self.rows += 1

    def hexdigest(self) -> str:
        h = hashlib.sha256()
        h.update(HASH_PREAMBLE)
        h.update(self._header)
        h.update(b"R%d;" % self.rows)
        h.update(self._sum.to_bytes(32, "big"))
        return "sha256:" + h.hexdigest()


_MASK_256 = (1 << 256) - 1


def stamped(
    receipt: Mapping[str, Any], *, table: str, rows: int, columns: list[str], content_hash: str
) -> dict[str, Any]:
    """The receipt with its table described in the core, as it is filed."""
    core = {
        **receipt["core"],
        "table": table,
        "rows": rows,
        "columns": list(columns),
        "content_hash": content_hash,
    }
    return {**receipt, "core": core}


def write_parquet(
    path: Path, table: pa.Table, receipt: Mapping[str, Any], *, name: str
) -> dict[str, Any]:
    """Write ``table`` to ``path`` with its receipt in the footer under
    :data:`RECEIPT_KEY`, and return the receipt as filed: its core describing
    the table it now travels with."""
    filed = stamped(
        receipt,
        table=name,
        rows=table.num_rows,
        columns=table.column_names,
        content_hash=Workspace.content_hash(table),
    )
    meta = dict(table.schema.metadata or {})
    meta[RECEIPT_KEY] = json.dumps(filed, sort_keys=True).encode()
    pq.write_table(table.replace_schema_metadata(meta), path, compression="zstd")
    return filed


def _cell_bytes(v: Any) -> bytes:
    """One cell of a :class:`TableHash` row."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return b"N"
    if isinstance(v, str):
        encoded = v.encode()
        return b"S%d:" % len(encoded) + encoded
    if isinstance(v, bool):
        return b"B1" if v else b"B0"
    if isinstance(v, int):
        return b"I%d;" % v
    if isinstance(v, (bytes, bytearray)):
        return b"X%d:" % len(v) + bytes(v)
    raise WorkspaceError(
        f"a {type(v).__name__} cell has no specified hash encoding: {v!r} "
        "(notes/session-format.md §5 covers null, string, integer, boolean and bytes)"
    )
