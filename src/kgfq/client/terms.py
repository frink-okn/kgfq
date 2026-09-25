"""RDF terms: doc 03 §3.3 request syntax in both directions, and the JSON term
objects that rows carry.

The request syntax is strict on purpose (brackets are required, not optional —
§3.3 explains the three ways inference goes wrong). This module never emits a
CURIE in a request: an IRI always goes out as ``<...>``, so a term's meaning can
never depend on which bundle's manifest it reaches. CURIEs are accepted on the
way *in* (from a user or an agent) against a prefix map the caller supplies, and
produced on the way *out* only for display.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
RDF_LANG_STRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"


class BadTermSyntax(ValueError):
    """A string that is not a term under §3.3, with the remedy in the message."""


@dataclass(frozen=True, slots=True)
class Iri:
    value: str

    def __str__(self) -> str:
        return f"<{self.value}>"


@dataclass(frozen=True, slots=True)
class Literal:
    value: str
    lang: str | None = None
    datatype: str | None = None

    def __post_init__(self) -> None:
        if self.lang is not None and self.datatype is not None:
            raise BadTermSyntax("a literal has a language tag or a datatype, not both")

    def __str__(self) -> str:
        return format_term(self)


@dataclass(frozen=True, slots=True)
class BNode:
    """Blank-node *syntax*. Parsed and carried, never matched by a server (§3.4.1)."""

    label: str

    def __str__(self) -> str:
        return f"_:{self.label}"


Term = Iri | Literal | BNode


@dataclass(frozen=True, slots=True)
class Var:
    """A named variable, for bodies where naming matters (bindings, star specs)."""

    name: str

    def __post_init__(self) -> None:
        if not _VAR_NAME.fullmatch(self.name):
            raise BadTermSyntax(f"variable name {self.name!r} is not a SPARQL variable name")

    def __str__(self) -> str:
        return f"?{self.name}"


_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CURIE = re.compile(r"([A-Za-z_][A-Za-z0-9_.\-]*)?:(.*)", re.DOTALL)
_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"(?:@([A-Za-z0-9\-]+)|\^\^(.+))?', re.DOTALL)
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def _unescape(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            out.append(_ESCAPES.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def expand_curie(curie: str, prefixes: Mapping[str, str]) -> str:
    """Expand ``prefix:local`` against ``prefixes``; §3.3's undeclared-prefix rule."""
    m = _CURIE.fullmatch(curie)
    if m is None:
        raise BadTermSyntax(f"{curie!r} is not a CURIE")
    prefix, local = m.group(1) or "", m.group(2)
    if prefix not in prefixes:
        raise BadTermSyntax(
            f"`{curie}` is a CURIE against the undeclared prefix `{prefix}`; if it is an IRI, "
            f"bracket it as `<{curie}>`, otherwise use a prefix the manifest declares"
        )
    return prefixes[prefix] + local


def parse_term(text: str, prefixes: Mapping[str, str] | None = None) -> Term | Var:
    """Parse §3.3 request syntax: ``<iri>``, ``prefix:local``, ``"literal"@en``,
    ``"42"^^xsd:integer``, ``_:b``, or ``?var``."""
    text = text.strip()
    if not text:
        raise BadTermSyntax("an empty string is not a term")
    if text.startswith("<") and text.endswith(">"):
        return Iri(text[1:-1])
    if text.startswith("?"):
        return Var(text[1:])
    if text.startswith("_:"):
        return BNode(text[2:])
    if text.startswith('"'):
        m = _LITERAL.fullmatch(text)
        if m is None:
            raise BadTermSyntax(f"{text!r} is not a well-formed literal")
        value, lang, dt = m.group(1), m.group(2), m.group(3)
        datatype = None
        if dt is not None:
            parsed = parse_term(dt, prefixes)
            if not isinstance(parsed, Iri):
                raise BadTermSyntax(f"datatype {dt!r} must be an IRI or a CURIE")
            datatype = parsed.value
        return Literal(_unescape(value), lang=lang, datatype=datatype)
    if ":" in text and not any(c.isspace() for c in text):
        return Iri(expand_curie(text, prefixes or {}))
    raise BadTermSyntax(
        f"{text!r} is neither a bracketed IRI, a CURIE, a quoted literal, nor a variable"
    )


def format_term(term: Term | Var) -> str:
    """§3.3 request syntax. IRIs are always bracketed; never a CURIE."""
    match term:
        case Iri(value):
            return f"<{value}>"
        case Literal(value, lang, datatype):
            body = f'"{_escape(value)}"'
            if lang:
                return f"{body}@{lang}"
            if datatype and datatype != XSD_STRING:
                return f"{body}^^<{datatype}>"
            return body
        case BNode(label):
            return f"_:{label}"
        case Var(name):
            return f"?{name}"
    raise TypeError(f"not a term: {term!r}")


def compact_iri(iri: str, prefixes: Mapping[str, str]) -> str:
    """Display form only: the longest declared namespace wins, else the full IRI."""
    best: tuple[int, str] | None = None
    for prefix, ns in prefixes.items():
        if iri.startswith(ns) and (best is None or len(ns) > best[0]):
            best = (len(ns), prefix)
    if best is None:
        return iri
    return f"{best[1]}:{iri[best[0] :]}"


def term_from_json(obj: Mapping[str, Any]) -> Term:
    """A SPARQL-Results-JSON-style term object from a response row."""
    kind = obj.get("type")
    value = obj.get("value")
    if not isinstance(value, str):
        raise ValueError(f"term object without a string value: {obj!r}")
    if kind in ("iri", "uri"):
        return Iri(value)
    if kind == "literal":
        return Literal(
            value, lang=obj.get("lang") or obj.get("xml:lang"), datatype=obj.get("datatype")
        )
    if kind == "bnode":
        return BNode(value)
    raise ValueError(f"unknown term type {kind!r} in {obj!r}")


def term_to_json(term: Term) -> dict[str, Any]:
    match term:
        case Iri(value):
            return {"type": "iri", "value": value}
        case Literal(value, lang, datatype):
            out: dict[str, Any] = {"type": "literal", "value": value}
            if lang:
                out["lang"] = lang
            if datatype:
                out["datatype"] = datatype
            return out
        case BNode(label):
            return {"type": "bnode", "value": label}
    raise TypeError(f"not a term: {term!r}")


TERM_KINDS = ("iri", "literal", "bnode")


def term_columns(
    term: Term | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """The ``(value, kind, lang, datatype)`` a table's column set stores for one
    term (doc 06 §6.3, plus ``kind``). The kind is stated, never inferred from
    which columns are null. A literal's datatype is stored only when it says
    something: RDF 1.1 makes a plain literal ``xsd:string`` and a tagged one
    ``rdf:langString``, so both are null here however the server spelled them —
    one term, one encoding. A blank node keeps its ``_:`` so that ``s``, which
    has no kind column, is never ambiguous."""
    match term:
        case None:
            return (None, None, None, None)
        case Iri(value):
            return (value, "iri", None, None)
        case Literal(value, lang, datatype):
            if lang or datatype in (XSD_STRING, RDF_LANG_STRING):
                datatype = None
            return (value, "literal", lang, datatype)
        case BNode(label):
            return (f"_:{label}", "bnode", None, None)
    raise TypeError(f"not a term: {term!r}")
