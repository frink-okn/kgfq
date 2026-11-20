"""``bind``: a pattern's variables take the values of an earlier table's
columns (``docs/session/operations.yaml``; ``../kgf-explorer``'s form).

``{"table": "chems", "columns": {"c": "chem"}}`` binds ``?c`` to the distinct
values of ``chems``'s column ``chem``; with several variables, to the distinct
tuples. It is how one step's result carries into the next without a file
between them, and without a value passing through a model: the terms are read
back from the table's own columns, kind, language and datatype included, so
what is sent is what was fetched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from ..client.terms import (
    BadTermSyntax,
    Term,
    Var,
    format_term,
    term_column_names,
    term_from_columns,
    variable_columns,
)
from .store import Workspace, WorkspaceError


def parse_bind(text: str) -> dict[str, Any]:
    """``chems:c=chem,g=gene`` as the registry's ``bind``. ``chems:chem`` binds
    ``?chem`` to the column of the same name."""
    table, sep, pairs = text.partition(":")
    columns: dict[str, str] = {}
    for pair in pairs.split(",") if sep else []:
        var, eq, column = (p.strip() for p in pair.partition("="))
        var = var.removeprefix("?")
        if not var or (eq and not column):
            columns = {}
            break
        columns[var] = column or var
    if not table.strip() or not columns:
        raise BadTermSyntax(
            f"--bind is `table:variable=column[,variable=column]` (`chems:c=chem`), got {text!r}"
        )
    for var in columns:
        Var(var)  # a variable name, or BadTermSyntax saying it is not
    return {"table": table.strip(), "columns": columns}


def bind_rows(ws: Workspace, bind: Mapping[str, Any]) -> tuple[list[str], list[list[Term]]]:
    """The variables and the distinct tuples ``bind`` gives them, in the table's
    order. A row with no value in a bound column binds nothing and is left out."""
    name, columns = str(bind["table"]), dict(bind["columns"])
    if not ws.has_table(name):
        raise WorkspaceError(
            f"no table {name!r} to bind from; tables so far: {', '.join(ws.list_tables()) or 'none'}"
        )
    shape = ws.read_receipt(name)["core"].get("shape")
    names = ws.table_columns(name)
    terms = term_column_names(names)
    for var, column in columns.items():
        if column not in terms:
            raise WorkspaceError(
                f"table {name} has no column {column!r} to bind ?{var} to; "
                f"its columns are {', '.join(terms) or 'none'}"
            )
    readers = []
    for column in columns.values():
        four = variable_columns(column)
        if all(c in names for c in four):
            readers.append(four)
        elif shape == "triples" and column in ("s", "p"):
            readers.append([column])  # an IRI, or a blank node that kept its `_:`
        else:
            raise WorkspaceError(
                f"table {name}'s column {column!r} has no `{column}_kind` beside it, so its "
                "values cannot be sent as terms"
            )
    table = ws.read_table(name, columns=[c for four in readers for c in four])
    seen: dict[tuple[str, ...], list[Term]] = {}
    lists = [[table.column(c).to_pylist() for c in four] for four in readers]
    for i in range(table.num_rows):
        row: list[Term] = []
        for cells in lists:
            value = cells[0][i]
            if value is None:
                break
            if len(cells) == 1:
                term = term_from_columns(value, "bnode" if value.startswith("_:") else "iri")
            else:
                term = term_from_columns(value, cells[1][i], cells[2][i], cells[3][i])
            row.append(term)
        else:
            seen.setdefault(tuple(format_term(t) for t in row), row)
    return list(columns), list(seen.values())


SAMPLE = 8


def unmatched_report(
    vars_: Sequence[str],
    rows: Sequence[Sequence[Term]],
    matched: set[tuple[str, ...]],
    *,
    complete: bool,
    bind: Mapping[str, Any] | None = None,
    values: str | None = None,
) -> dict[str, Any]:
    """How many of the input tuples found something, and a sample of those that
    found nothing (plan C6, 2026-09-24; ``../kgf-explorer`` D8). It is the
    number to read before trusting the next join: a value that matched nothing
    may be absent, or may be spelled another way than the graph spells it, and
    only a look at the sample tells which.

    ``matched`` holds the input tuples, in request syntax, that some row or
    count answered. Over the distinct tuples, since one given twice is sent
    once. A read that stopped early cannot say what the rest would have
    matched, so it reports what it saw and leaves ``unmatched`` null."""
    sent: dict[tuple[str, ...], None] = {}
    for r in rows:
        sent.setdefault(tuple(format_term(t) for t in r))
    missing = [t for t in sent if t not in matched]
    report: dict[str, Any] = {"vars": list(vars_), "sent": len(sent)}
    if bind:
        report.update(table=bind["table"], columns=dict(bind["columns"]))
    if values:
        report["values"] = values
    report["matched"] = len(sent) - len(missing)
    report["unmatched"] = len(missing) if complete else None
    report["unmatched_sample"] = [list(t) for t in missing[:SAMPLE]] if complete else []
    return report


def tuples_in(table: pa.Table, vars_: Sequence[str]) -> set[tuple[str, ...]]:
    """The distinct tuples a fetched table holds for these variables, in request
    syntax, read from their four columns each."""
    columns = [[table.column(c).to_pylist() for c in variable_columns(v)] for v in vars_]
    out: set[tuple[str, ...]] = set()
    for i in range(table.num_rows):
        out.add(
            tuple(
                format_term(term_from_columns(value[i], kind[i], lang[i], datatype[i]))
                for value, kind, lang, datatype in columns
            )
        )
    return out
