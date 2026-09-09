"""The SPARQL linter (plan §5.6): ``KGFQ001``–``KGFQ012``, usable standalone
(`kgfq lint`), by registry CI, and by ``kgfq sparql`` before it launches
``kgf-sparql``. It parses algebra, names what the syntax hides, and never
rewrites."""

from __future__ import annotations

from .analysis import ParseError, QueryShape, analyze
from .header import datasets_of, parse_header
from .rules import RULES, Diagnostic, lint_shape

__all__ = [
    "RULES",
    "Diagnostic",
    "ParseError",
    "QueryShape",
    "analyze",
    "datasets_of",
    "lint",
    "parse_header",
]


def lint(
    text: str,
    *,
    threshold: int = 6,
    closed_predicates: set[str] | None = None,
    label_predicates: set[str] | None = None,
) -> list[Diagnostic]:
    shape = analyze(text)
    return lint_shape(
        shape,
        threshold=threshold,
        closed_predicates=closed_predicates,
        label_predicates=label_predicates,
    )
