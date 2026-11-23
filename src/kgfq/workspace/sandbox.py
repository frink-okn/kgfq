"""The sandboxed DuckDB (plan C8, D6): the one module that opens one.

``select`` runs a single SELECT over the workspace's tables and nothing else.
The statement is as likely a model's as a person's, and the tables hold text
from anywhere: a literal can carry instructions, and an agent with a SQL tool
can be steered by what it reads. So the engine is given no capability that
would make that harmful, and the refusals are DuckDB's own, not a filter of
ours over the statement's text:

- **Nothing outside the workspace is readable.** ``enable_external_access`` is
  off: no file, no URL, no glob. The tables come in as Arrow datasets this
  module registers, read by pyarrow, so DuckDB itself opens nothing.
- **No extension loads**, and Python's variables are not tables
  (``python_enable_replacements``).
- **The configuration is locked** before the statement runs, so no statement
  can turn any of that back on.
- **One statement, and it is a SELECT**, by the parser's own account of what it
  parsed. ``WITH … SELECT`` is one.
- **Identifiers are the workspace's own table names**, checked when the table
  was written. Nothing from a statement or a graph is spliced into SQL here.
- **It is bounded**: a time budget that interrupts the query, a row budget on
  what is read back, and a scratch directory of its own for what spills.

The connection is in memory, one per statement, with only the tables the
statement names: the Parquet files stay the store (D15), and two processes
never contend for a database file.
"""

from __future__ import annotations

import datetime
import decimal
import json
import math
import threading
from dataclasses import dataclass
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds

from ..client.errors import KgfqError
from ..client.terms import (
    COMPANIONS,
    TERM_KINDS,
    Literal,
    Term,
    term_column_names,
    term_from_columns,
    variable_columns,
)
from .store import Workspace

XSD = "http://www.w3.org/2001/XMLSchema#"
MAX_ROWS = 1_000_000
TIMEOUT_S = 60.0
SETTINGS = (
    "SET python_enable_replacements = false",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET enable_external_access = false",
    "SET lock_configuration = true",
)


class SqlRefused(KgfqError):
    """A statement the sandbox would not run, or DuckDB would not: the message
    is DuckDB's own where it has one."""


@dataclass(slots=True)
class Selected:
    """What one SELECT gave: its rows as DuckDB typed them, the workspace
    tables it read, and whether the row budget cut it short."""

    table: pa.Table
    reads: list[str]
    truncated: bool
    duckdb: str


def _one_select(statement: str) -> str:
    """The statement's text, if it is exactly one SELECT."""
    text = statement.strip()
    if not text:
        raise SqlRefused("sql needs a SELECT statement")
    try:
        parsed = duckdb.extract_statements(text)
    except duckdb.Error as e:
        raise SqlRefused(f"Only a single SELECT is run here. DuckDB says: {e}") from None
    if len(parsed) != 1 or parsed[0].type != duckdb.StatementType.SELECT:
        kinds = ", ".join(s.type.name for s in parsed) or "nothing"
        raise SqlRefused(
            f"Only a single SELECT is run here (WITH … SELECT is one); this is {kinds}. "
            "A result is kept by naming it: it is filed as a table."
        )
    return parsed[0].query


def _named(node: Any, out: set[str]) -> None:
    """Every base table a parsed statement names, wherever it names it."""
    if isinstance(node, dict):
        if node.get("type") == "BASE_TABLE" and node.get("table_name"):
            out.add(str(node["table_name"]).lower())
        for value in node.values():
            _named(value, out)
    elif isinstance(node, list):
        for value in node:
            _named(value, out)


def _reads(con: duckdb.DuckDBPyConnection, ws: Workspace, text: str) -> list[str]:
    """The workspace tables a statement names, from DuckDB's own parse of it
    (``json_serialize_sql``), on the connection that is already locked.

    It has to be a parse and nothing more. ``get_table_names`` binds as it
    goes: on a connection that was not locked it opened the file a
    ``read_parquet(…)`` named before the sandbox had seen the statement, and it
    cannot bind a ``JOIN … USING`` at all. The tree also names CTEs and tables
    that do not exist; those are not ours, and DuckDB says so itself when the
    statement runs."""
    [(serialized,)] = con.execute("SELECT json_serialize_sql(?)", [text]).fetchall()
    tree = json.loads(serialized)
    if tree.get("error"):
        raise SqlRefused(
            f"Only a single SELECT is run here. DuckDB says: {tree.get('error_message')}"
        )
    named: set[str] = set()
    _named(tree, named)
    by_lower: dict[str, list[str]] = {}
    for name in ws.list_tables():
        by_lower.setdefault(name.lower(), []).append(name)
    reads = []
    for lower in sorted(named):
        candidates = by_lower.get(lower, [])
        if len(candidates) > 1:
            raise SqlRefused(
                f"tables {', '.join(candidates)} differ only by case, and SQL cannot tell them apart"
            )
        reads.extend(candidates)
    return reads


def select(
    ws: Workspace, statement: str, *, max_rows: int = MAX_ROWS, timeout_s: float = TIMEOUT_S
) -> Selected:
    """Run one SELECT over the workspace's tables, locked down as this module's
    docstring says, and return at most ``max_rows`` rows. The lock comes first:
    the statement is not so much as parsed for its tables until it is on."""
    text = _one_select(statement)
    con = duckdb.connect(":memory:")
    timer: threading.Timer | None = None
    timed_out = threading.Event()

    def stop() -> None:
        timed_out.set()
        con.interrupt()

    try:
        # the scratch directory can only be named while external access is on
        con.execute("SET temp_directory = ?", [str(ws.scratch_dir() / "duckdb")])
        for setting in SETTINGS:
            con.execute(setting)
        version = str(con.execute("SELECT version()").fetchall()[0][0])
        try:
            reads = _reads(con, ws, text)
            for name in reads:
                con.register(name, ds.dataset(ws.table_path(name), format="parquet"))
            timer = threading.Timer(timeout_s, stop)
            timer.start()
            reader = con.execute(text).to_arrow_reader(10_000)
            batches: list[pa.RecordBatch] = []
            rows = 0
            for batch in reader:
                batches.append(batch)
                rows += batch.num_rows
                if rows > max_rows:
                    break
            schema = reader.schema
        except duckdb.Error as e:
            if timed_out.is_set():
                raise SqlRefused(
                    f"stopped at the time budget of {timeout_s:g} s. Filter earlier, or join on "
                    "fewer rows; a join with no condition multiplies its tables."
                ) from None
            raise SqlRefused(str(e).strip()) from None
    finally:
        if timer is not None:
            timer.cancel()
        con.close()
    table = pa.Table.from_batches(batches, schema=schema)
    truncated = table.num_rows > max_rows
    return Selected(table.slice(0, max_rows) if truncated else table, reads, truncated, version)


def js_number(x: float) -> str:
    """A double as ECMAScript's ``Number::toString`` writes it, so that a table
    kgfq derives and one ``../kgf-explorer`` derives spell a number alike:
    the shortest digits that round-trip, plain notation from 1e-6 up to 1e21,
    and ``1e+21``, ``1.5e-7`` beyond. Python's ``repr`` has the same digits and
    other switches (``1e+16``, ``1e-07``)."""
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign, digits, exponent = decimal.Decimal(repr(x)).as_tuple()
    assert isinstance(exponent, int)  # a finite number's exponent is one
    text = "".join(map(str, digits)).rstrip("0")
    exponent += len(digits) - len(text)
    k, n = len(text), len(text) + exponent
    if k <= n <= 21:
        body = text + "0" * (n - k)
    elif 0 < n <= 21:
        body = f"{text[:n]}.{text[n:]}"
    elif -6 < n <= 0:
        body = "0." + "0" * -n + text
    else:
        e = n - 1
        mantissa = text if k == 1 else f"{text[0]}.{text[1:]}"
        body = f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
    return ("-" if sign else "") + body


def computed_literal(value: Any, kind: pa.DataType) -> Literal | None:
    """A value SQL computed, not read from a term column, as a literal of its
    type: text with a datatype beside it. So every stored cell is text, a table
    has one layout whoever derived it, and the content hash needs no spelling
    for a number of its own (C6)."""
    if value is None:
        return None
    if pa.types.is_boolean(kind):
        return Literal("true" if value else "false", datatype=XSD + "boolean")
    if pa.types.is_integer(kind):
        return Literal(str(value), datatype=XSD + "integer")
    if pa.types.is_decimal(kind):
        return Literal(format(value, "f"), datatype=XSD + "decimal")
    if pa.types.is_floating(kind):
        return Literal(js_number(float(value)), datatype=XSD + "double")
    if pa.types.is_date(kind):
        return Literal(value.isoformat(), datatype=XSD + "date")
    if pa.types.is_timestamp(kind):
        return Literal(value.isoformat(), datatype=XSD + "dateTime")
    if pa.types.is_string(kind) or pa.types.is_large_string(kind):
        return Literal(str(value))
    if isinstance(value, (datetime.time, datetime.timedelta, bytes)):
        return Literal(str(value))
    return Literal(json.dumps(value, default=str, ensure_ascii=False))


def _holding(cells: pa.Table, column: str, wanted: pa.Array) -> pa.Table:
    """The rows whose ``column`` is one of ``wanted``."""
    mask = pc.is_in(cells.column(column), value_set=wanted)  # ty: ignore[unresolved-attribute] — pyarrow generates its compute functions at import
    return cells.filter(mask)


def _encodings(ws: Workspace, reads: list[str], values: set[str]) -> dict[str, set[tuple]]:
    """For each of ``values``, the encodings (kind, language, datatype) it has
    in the term columns of the tables read."""
    found: dict[str, set[tuple]] = {}
    if not values:
        return found
    wanted = pa.array(sorted(values), type=pa.string())
    for name in reads:
        names = ws.table_columns(name)
        shape = ws.read_receipt(name)["core"].get("shape")
        for column in term_column_names(names):
            four = variable_columns(column)
            if all(c in names for c in four):
                cells = ws.read_table(name, columns=four)
                hit = _holding(cells, column, wanted)
                seen = set(zip(*(hit.column(c).to_pylist() for c in four), strict=True))
            elif shape == "triples" and column in ("s", "p"):
                cells = ws.read_table(name, columns=[column])
                hit = _holding(cells, column, wanted)
                seen = {
                    (v, "bnode" if v.startswith("_:") else "iri", None, None)
                    for v in hit.column(column).to_pylist()
                }
            else:
                continue
            for value, *encoding in seen:
                found.setdefault(value, set()).add(tuple(encoding))
    return found


def terms_from_sql(
    ws: Workspace, selected: Selected
) -> tuple[list[str], list[dict[str, Term | None]], dict[str, int]]:
    """A SELECT's rows back as terms: the variables, the rows, and per column
    how many text values fell to plain text.

    The rule is ``../kgf-explorer``'s (its D6), so the two derive one table. A
    column ``x`` that arrives with ``x_kind`` is reassembled from its
    companions. A text column without them takes the kind its values have in
    the tables the query read, where a value has exactly one encoding there.
    Other text is a plain literal, and is counted, because an IRI built in SQL
    is text and a fetch bound to it matches nothing. A computed value is a
    literal of its type. A kind is stated or found; it is never read off a
    value's shape."""
    table = selected.table
    names = table.column_names
    if len(set(names)) != len(names):
        twice = sorted({n for n in names if names.count(n) > 1})
        raise SqlRefused(
            f"the result has more than one column called {', '.join(twice)}; name them with AS"
        )
    explicit = {n for n in names if f"{n}_kind" in names}
    companions = {f"{n}_{c}" for n in explicit for c in COMPANIONS}
    variables = [n for n in names if n not in companions]
    columns = {n: table.column(n).to_pylist() for n in names}
    kinds = {n: table.schema.field(n).type for n in names}
    text_columns = [
        v
        for v in variables
        if v not in explicit
        and (pa.types.is_string(kinds[v]) or pa.types.is_large_string(kinds[v]))
    ]
    encodings = _encodings(
        ws, selected.reads, {x for v in text_columns for x in columns[v] if x is not None}
    )
    plain: dict[str, int] = {}
    rows: list[dict[str, Term | None]] = []
    for i in range(table.num_rows):
        row: dict[str, Term | None] = {}
        for v in variables:
            value = columns[v][i]
            if value is None:
                row[v] = None
            elif v in explicit:
                kind = columns[f"{v}_kind"][i]
                if kind not in TERM_KINDS:
                    raise SqlRefused(
                        f"row {i + 1}: {v}_kind is {kind!r}; a kind is one of {', '.join(TERM_KINDS)}"
                    )
                lang = columns[f"{v}_lang"][i] if f"{v}_lang" in columns else None
                datatype = columns[f"{v}_datatype"][i] if f"{v}_datatype" in columns else None
                row[v] = term_from_columns(str(value), kind, lang, datatype)
            elif v in text_columns:
                seen = encodings.get(value, set())
                if len(seen) == 1:
                    row[v] = term_from_columns(value, *next(iter(seen)))
                else:
                    plain[v] = plain.get(v, 0) + 1
                    row[v] = Literal(value)
            else:
                row[v] = computed_literal(value, kinds[v])
        rows.append(row)
    return variables, rows, plain
