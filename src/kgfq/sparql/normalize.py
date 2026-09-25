"""``--normalize`` (plan §5.4): the two proven-equivalent rewrites, each one
recorded in the receipt with the reason it was or was not applied.

- **KGFQ002.** ``BIND(<constant> AS ?v)`` becomes ``VALUES ?v { <constant> }``
  in the same place. ``Extend(P, ?v, c)`` with ``?v`` not in ``P`` denotes
  ``Join(P, {?v ↦ c})``, which is what the inline data block at that position
  denotes.
- **KGFQ001.** ``VALUES ?v { c1 … cn }`` is inserted at the top of the query's
  outer group and the FILTER stays as the residual check. The rule fires only
  for a FILTER in the required part on a variable bound by a required pattern;
  this module additionally proves from the filter's own structure that its
  truth implies ``?v ∈ {c1 … cn}`` (``=`` and ``IN`` under ``&&`` and ``||``,
  nothing under ``!``). Every solution of the original then already satisfies
  the new block and matches exactly one of its deduplicated rows, so the
  multiset is unchanged. ``VALUES`` matches by term where ``=`` compares typed
  literals by value, so the rewrite is made only for IRIs and string literals.

Every rewrite is verified by re-parsing and re-linting the result: one after
which the diagnostic still fires, or which no longer parses, is reverted and
reported. Nothing else is ever rewritten (client-suite §6.2)."""

from __future__ import annotations

import re
from typing import Any

from rdflib import XSD, Literal, URIRef, Variable
from rdflib.plugins.sparql.parserutils import CompValue

from ..lint import Diagnostic, ParseError, analyze, lint_shape
from ..lint.analysis import format_term, parse

_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)


def normalize(
    text: str,
    diagnostics: list[Diagnostic],
    *,
    threshold: int = 6,
    closed_predicates: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Apply the rewrites the diagnostics license; return the new text and one
    record per candidate, applied or not."""
    records: list[dict[str, Any]] = []
    current = text
    for d in diagnostics:
        if d.code == "KGFQ002":
            current, record = _bind_to_values(current, d, threshold, closed_predicates)
            records.append(record)
    for d in diagnostics:
        if d.code == "KGFQ001":
            current, record = _filter_to_values(current, d, threshold, closed_predicates)
            records.append(record)
    return current, records


# -- KGFQ002 ---------------------------------------------------------------


def _bind_to_values(
    text: str, d: Diagnostic, threshold: int, closed: set[str] | None
) -> tuple[str, dict[str, Any]]:
    var = str(d.detail.get("variable", "")).lstrip("?")
    record: dict[str, Any] = {"rule": "KGFQ002", "variable": f"?{var}", "applied": False}
    masked = _mask(text)
    pattern = re.compile(
        r"\bBIND\s*\(\s*(?P<expr>.+?)\s+AS\s+\?" + re.escape(var) + r"\s*\)",
        re.IGNORECASE | re.DOTALL,
    )
    matches = list(pattern.finditer(masked))
    if len(matches) != 1:
        record["reason"] = f"{len(matches)} BIND(… AS ?{var}) candidates in the text; expected one"
        return text, record
    m = matches[0]
    constant = text[m.start("expr") : m.end("expr")].strip()
    replacement = f"VALUES ?{var} {{ {constant} }}"
    candidate = text[: m.start()] + replacement + text[m.end() :]
    record["rewrite"] = f"replaced {text[m.start() : m.end()]} with {replacement}"
    record["equivalence"] = (
        "Extend(P, ?v, c) with ?v not in P denotes Join(P, {?v ↦ c}), the inline data block at the same position"
    )
    return _verify(
        text, candidate, record, code="KGFQ002", var=var, threshold=threshold, closed=closed
    )


# -- KGFQ001 ---------------------------------------------------------------


def _filter_to_values(
    text: str, d: Diagnostic, threshold: int, closed: set[str] | None
) -> tuple[str, dict[str, Any]]:
    var = str(d.detail.get("variable", "")).lstrip("?")
    record: dict[str, Any] = {"rule": "KGFQ001", "variable": f"?{var}", "applied": False}
    try:
        parsed, _ = parse(text)
        shape = analyze(text)
    except ParseError as e:
        record["reason"] = f"does not parse: {e}"
        return text, record
    v = Variable(var)
    guaranteed: list[Any] | None = None
    for f in shape.filters:
        if not f.required or v not in f.equalities:
            continue
        sets = _guaranteed(f.expr)
        if v in sets:
            guaranteed = sets[v]
            break
    if guaranteed is None:
        record["reason"] = (
            f"the FILTER's truth does not imply a finite set for ?{var} (a disjunction across variables, or a negation)"
        )
        return text, record
    ineligible = [c for c in guaranteed if not _by_term(c)]
    if ineligible:
        record["reason"] = (
            f"{', '.join(format_term(c) for c in ineligible)} compares by value under `=` and by term under VALUES"
        )
        return text, record
    values = " ".join(dict.fromkeys(format_term(c) for c in guaranteed))
    block = f"VALUES ?{var} {{ {values} }}"
    at = _outer_group_open(text, str(getattr(parsed[1], "name", "")))
    if at is None:
        record["reason"] = "could not find the query's outer group"
        return text, record
    candidate = text[: at + 1] + f"\n  {block}" + text[at + 1 :]
    record["rewrite"] = (
        f"inserted {block} at the top of the outer group; the FILTER stays as the residual check"
    )
    record["equivalence"] = (
        "?v is bound by a required pattern and the FILTER implies ?v ∈ the set, so every solution matches exactly one deduplicated VALUES row"
    )
    return _verify(
        text, candidate, record, code="KGFQ001", var=var, threshold=threshold, closed=closed
    )


def _guaranteed(expr: Any) -> dict[Variable, list[Any]]:
    """For each variable, a finite set its value is in whenever ``expr`` is true."""
    if not isinstance(expr, CompValue):
        return {}
    name = expr.name
    if name == "RelationalExpression":
        left, op, right = expr.get("expr"), expr.get("op"), expr.get("other")
        if isinstance(left, Variable) and op == "=" and isinstance(right, (URIRef, Literal)):
            return {left: [right]}
        if (
            isinstance(left, Variable)
            and op == "IN"
            and isinstance(right, (list, tuple))
            and right
            and all(isinstance(r, (URIRef, Literal)) for r in right)
        ):
            return {left: list(right)}
        return {}
    parts = [expr.get("expr"), *(expr.get("other") or [])]
    if name == "ConditionalAndExpression":
        out: dict[Variable, list[Any]] = {}
        for part in parts:
            for var, consts in _guaranteed(part).items():
                out.setdefault(var, consts)
        return out
    if name == "ConditionalOrExpression":
        branches = [_guaranteed(p) for p in parts]
        common = set.intersection(*(set(b) for b in branches)) if branches else set()
        return {var: [c for b in branches for c in b[var]] for var in common}
    return {}


def _by_term(c: Any) -> bool:
    """``=`` and VALUES agree on IRIs and on string literals, not on typed ones."""
    if isinstance(c, URIRef):
        return True
    if isinstance(c, Literal):
        return bool(c.language) or c.datatype is None or c.datatype == XSD.string
    return False


# -- verification -----------------------------------------------------------


def _verify(
    original: str,
    candidate: str,
    record: dict[str, Any],
    *,
    code: str,
    var: str,
    threshold: int,
    closed: set[str] | None,
) -> tuple[str, dict[str, Any]]:
    try:
        shape = analyze(candidate)
    except ParseError as e:
        record["reason"] = f"reverted: the rewritten query does not parse: {e}"
        return original, record
    if Variable(var) not in shape.values_vars:
        record["reason"] = "reverted: the rewritten query does not bind the variable by VALUES"
        return original, record
    remaining = lint_shape(shape, threshold=threshold, closed_predicates=closed)
    if any(x.code == code and x.detail.get("variable") == f"?{var}" for x in remaining):
        record["reason"] = f"reverted: {code} still fires for ?{var} after the rewrite"
        return original, record
    record["applied"] = True
    return candidate, record


# -- text -------------------------------------------------------------------


def _mask(text: str) -> str:
    """The text with comment and string contents blanked to spaces, same
    length, so a regular expression over it finds only code, and its offsets
    index the original."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif c in "\"'":
            quote = text[i : i + 3] if text[i : i + 3] in ('"""', "'''") else c
            i += len(quote)
            while i < n and text[i : i + len(quote)] != quote:
                if text[i] == "\\":
                    out[i] = " "
                    i += 1
                if i < n and text[i] != "\n":
                    out[i] = " "
                i += 1
            i += len(quote)
        elif c == "<":
            j = text.find(">", i)
            span = text[i + 1 : j] if j > 0 else ""
            if j > 0 and span and not re.search(r"[\s<{}\"|^`\\]", span):
                i = j + 1  # an IRI: kept, and its `#` is not a comment
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


def _outer_group_open(text: str, form: str) -> int | None:
    """The index of the ``{`` opening the query's pattern group: after WHERE
    when the query has one, else the first brace (SELECT and ASK allow the
    keyword to be omitted; CONSTRUCT's template comes first, so WHERE is
    required to find its group)."""
    masked = _mask(text)
    m = _WHERE.search(masked)
    if m:
        at = masked.find("{", m.end())
        return at if at >= 0 else None
    if form in ("ConstructQuery", "DescribeQuery"):
        return None
    at = masked.find("{")
    return at if at >= 0 else None
