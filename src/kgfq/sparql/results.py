"""Reading the engine's output into workspace tables (C6). SELECT rows are
bindings: four columns per variable, as a fetched table has them. CONSTRUCT and
DESCRIBE results are triples, under ``s, p, o`` and the object's companions.
``term_columns`` is the one encoding of a term, so the content hash does not
depend on whether the engine wrote a plain literal's datatype out."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import rdflib

from ..client.terms import (
    BNode,
    Iri,
    Literal,
    Term,
    term_columns,
    term_from_json,
    variable_columns,
)
from .engine import EngineRun

TRIPLE_COLUMNS = ["s", "p", "o", "o_kind", "o_lang", "o_datatype"]


class ResultError(ValueError):
    """The engine's output is not a document this module can read."""


def _columns(
    obj: Mapping[str, Any] | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    if obj is None:
        return (None, None, None, None)
    if obj.get("type") == "triple":
        raise ResultError("an RDF-star triple term has no table encoding")
    return term_columns(term_from_json(obj))


def bindings_table(doc: Mapping[str, Any]) -> pa.Table:
    """A SPARQL Results JSON document as an Arrow table, four columns per variable."""
    try:
        vars_ = [str(v) for v in doc["head"]["vars"]]
        rows = doc["results"]["bindings"]
    except (KeyError, TypeError) as e:
        raise ResultError(f"not a SPARQL Results JSON document: {e}") from None
    columns: dict[str, list[str | None]] = {c: [] for v in vars_ for c in variable_columns(v)}
    for row in rows:
        for v in vars_:
            value, kind, lang, datatype = _columns(row.get(v))
            columns[v].append(value)
            columns[f"{v}_kind"].append(kind)
            columns[f"{v}_lang"].append(lang)
            columns[f"{v}_datatype"].append(datatype)
    return pa.table({c: pa.array(vals, type=pa.string()) for c, vals in columns.items()})


def _rdflib_term(term: Any) -> Term:
    """An rdflib term as this library's, so `term_columns` stays the one encoding."""
    if isinstance(term, rdflib.URIRef):
        return Iri(str(term))
    if isinstance(term, rdflib.BNode):
        return BNode(str(term))
    if isinstance(term, rdflib.Literal):
        lang = str(term.language) if term.language else None
        datatype = str(term.datatype) if term.datatype and not lang else None
        return Literal(str(term), lang=lang, datatype=datatype)
    raise ResultError(f"not a term: {term!r}")


def triples_table(path: Path) -> pa.Table:
    """An N-Triples file as a triples table."""
    graph = rdflib.Graph()
    try:
        graph.parse(path, format="nt")
    except Exception as e:  # rdflib raises its own hierarchy
        raise ResultError(f"not N-Triples: {e}") from e
    columns: dict[str, list[str | None]] = {c: [] for c in TRIPLE_COLUMNS}
    for s, p, o in sorted(graph, key=lambda t: tuple(x.n3() for x in t)):
        columns["s"].append(term_columns(_rdflib_term(s))[0])
        columns["p"].append(term_columns(_rdflib_term(p))[0])
        ov, ok, ol, od = term_columns(_rdflib_term(o))
        columns["o"].append(ov)
        columns["o_kind"].append(ok)
        columns["o_lang"].append(ol)
        columns["o_datatype"].append(od)
    return pa.table({c: pa.array(vals, type=pa.string()) for c, vals in columns.items()})


def read_output(run: EngineRun) -> tuple[str, pa.Table | bool]:
    """The engine's output by the result type its receipt names: ``bindings`` as
    a table, ``quads`` as a triples table, ``boolean`` as the answer. Only a
    complete run's output, or one ended by the row budget, is a well-formed
    document; the caller decides when to read."""
    outcome = (run.receipt or {}).get("outcome", {})
    kind = str(outcome.get("result_type") or "bindings")
    if not run.output.exists():
        raise ResultError(f"kgf-sparql wrote no output at {run.output}")
    if kind == "quads":
        return kind, triples_table(run.output)
    try:
        doc = json.loads(run.output.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ResultError(f"the output is not a complete JSON document: {e}") from None
    if kind == "boolean":
        if "boolean" not in doc:
            raise ResultError("an ASK result without a boolean")
        return kind, bool(doc["boolean"])
    return kind, bindings_table(doc)
