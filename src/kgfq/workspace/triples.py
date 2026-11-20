"""A table back to the triples it stands for (plan C6, 2026-11-20).

A fetched table is its pattern's bindings: a column set per variable, none for
a constant. The receipt carries the pattern, so each row is still exactly one
triple, its values put back where the pattern's variables stand. That is what
``cite`` (C11) looks up and what an RDF tool reads; ``kgfq export`` writes it
as N-Triples. A SELECT or SQL result answers no single pattern and has no such
reading, which :func:`triples` says instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import IO, Any

import pyarrow as pa

from ..client.terms import (
    TRIPLE_COLUMNS,
    BNode,
    Iri,
    Literal,
    Var,
    format_term,
    parse_term,
    term_columns,
    variable_columns,
)
from .store import WorkspaceError


def triples(table: pa.Table, receipt: Mapping[str, Any]) -> pa.Table:
    """The triples ``table`` stands for, under :data:`TRIPLE_COLUMNS`, in the
    table's row order. A ``triples`` table is its own answer; a ``bindings``
    table needs the one pattern it answers, from its receipt."""
    name = receipt.get("table") or "the table"
    shape = receipt.get("shape")
    if shape == "triples":
        return table.select(TRIPLE_COLUMNS)
    pattern = receipt.get("pattern")
    if shape != "bindings" or not pattern:
        raise WorkspaceError(
            f"{name} answers no single pattern (shape {shape!r}, kind {receipt.get('kind')!r}), "
            "so its rows are not triples: a SELECT or SQL result joins several"
        )
    n = table.num_rows
    cells: dict[str, list[pa.Array | pa.ChunkedArray]] = {}
    for pos in ("s", "p", "o"):
        term = parse_term(str(pattern[pos]))
        if isinstance(term, Var):
            missing = [c for c in variable_columns(term.name) if c not in table.column_names]
            if missing:
                raise WorkspaceError(
                    f"{name}: its pattern names ?{term.name}, and it has no column "
                    f"{', '.join(missing)}"
                )
            cells[pos] = [table.column(c) for c in variable_columns(term.name)]
        else:
            cells[pos] = [
                pa.nulls(n, pa.string()) if v is None else pa.repeat(pa.scalar(v, pa.string()), n)
                for v in term_columns(term)
            ]
    (s, *_), (p, *_), (o, o_kind, o_lang, o_datatype) = cells["s"], cells["p"], cells["o"]
    return pa.table(dict(zip(TRIPLE_COLUMNS, [s, p, o, o_kind, o_lang, o_datatype], strict=True)))


def write_ntriples(table: pa.Table, out: IO[str]) -> int:
    """A triples table as N-Triples, one line a row; the number written."""
    written = 0
    for s, p, o, kind, lang, datatype in zip(
        *(table.column(c).to_pylist() for c in TRIPLE_COLUMNS), strict=True
    ):
        subject = s if s.startswith("_:") else _iri(s)
        if kind == "iri":
            obj = _iri(o)
        elif kind == "bnode":
            obj = format_term(BNode(o.removeprefix("_:")))
        else:
            obj = format_term(Literal(o, lang=lang, datatype=None if lang else datatype))
        out.write(f"{subject} {_iri(p)} {obj} .\n")
        written += 1
    return written


# IRIREF excludes these and everything at or below a space; N-Triples spells
# them as \uXXXX, so a graph's malformed IRI still round-trips as one line.
_IRI_UNSAFE = set('<>"{}|^`\\')


def _iri(value: str) -> str:
    if any(ch <= " " or ch in _IRI_UNSAFE for ch in value):
        value = "".join(
            f"\\u{ord(ch):04X}" if ch <= " " or ch in _IRI_UNSAFE else ch for ch in value
        )
        return f"<{value}>"
    return format_term(Iri(value))
