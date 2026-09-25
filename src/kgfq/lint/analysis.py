"""Query shape extraction for the linter (plan §5.6): rdflib's SPARQL algebra,
walked once into the handful of facts the rules read — required and optional
triple patterns, unions, filters classified by what they constrain, constant
binds, values, paths, and the query's modifiers.

The algebra is the right level: the rules are about what the query denotes
(a disconnected required pattern *is* a Cartesian product), not about how it
was spelled, and rdflib already resolves prefixes and property-path syntax.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rdflib import Literal, URIRef, Variable
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, Path, SequencePath
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

Term = Any  # rdflib terms; Variable, URIRef, Literal, BNode, or a Path in predicate position


class ParseError(ValueError):
    """The text is not a SPARQL query rdflib can parse."""


@dataclass(slots=True)
class TriplePattern:
    s: Term
    p: Term
    o: Term
    required: bool
    optional_group: int | None = None
    union_group: int | None = None
    index: int = 0

    def variables(self) -> set[Variable]:
        return {t for t in (self.s, self.p, self.o) if isinstance(t, Variable)}

    def path(self) -> Path | None:
        return self.p if isinstance(self.p, Path) else None


@dataclass(slots=True)
class FilterInfo:
    """One FILTER, classified. ``equalities`` maps a variable to the constants
    it is compared equal to, across ``||`` and ``IN``; ``text`` lists text
    constraints as (variable, function, wrapped-in-STR); ``vars`` is every
    variable the expression mentions."""

    vars: set[Variable]
    equalities: dict[Variable, list[Term]]
    text: list[tuple[Variable, str, bool]]
    required: bool
    expr: Any = None


@dataclass(slots=True)
class Node:
    """A unit of the required pattern for connectivity: a triple pattern, a
    UNION, a subselect, or a SERVICE block, with the variables it binds."""

    kind: str
    vars: set[Variable]
    patterns: list[TriplePattern]


@dataclass(slots=True)
class QueryShape:
    patterns: list[TriplePattern] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    optional_groups: list[list[TriplePattern]] = field(default_factory=list)
    filters: list[FilterInfo] = field(default_factory=list)
    binds: list[tuple[Variable, Term, bool]] = field(default_factory=list)
    values_vars: set[Variable] = field(default_factory=set)
    distinct: bool = False
    order_by: bool = False
    group_by: bool = False
    aggregates: bool = False
    limit: int | None = None
    select_star: bool = False
    projection: list[Variable] = field(default_factory=list)
    subselects: int = 0
    services: int = 0

    @property
    def required_patterns(self) -> list[TriplePattern]:
        return [p for p in self.patterns if p.required]

    def bound_vars(self) -> set[Variable]:
        """Definitely bound: by a required pattern or a VALUES block."""
        out = set(self.values_vars)
        for p in self.required_patterns:
            out |= p.variables()
        return out

    def constant_bound(self, var: Variable) -> bool:
        """The variable is pinned to constants: by VALUES, a constant BIND, or a
        required pattern whose other positions are constants (``?s <p> <o>``)."""
        if var in self.values_vars:
            return True
        if any(v == var and const for v, _, const in self.binds):
            return True
        for p in self.required_patterns:
            if var in p.variables() and len(p.variables()) == 1 and not p.path():
                return True
        return False


LABEL_PREDICATES = {
    URIRef("http://www.w3.org/2000/01/rdf-schema#label"),
    URIRef("http://www.w3.org/2004/02/skos/core#prefLabel"),
    URIRef("http://www.w3.org/2004/02/skos/core#altLabel"),
    URIRef("https://schema.org/name"),
    URIRef("http://schema.org/name"),
    URIRef("http://purl.org/dc/terms/title"),
    URIRef("http://purl.org/dc/elements/1.1/title"),
    URIRef("http://xmlns.com/foaf/0.1/name"),
    URIRef("http://www.w3.org/2000/01/rdf-schema#comment"),
}
RDF_TYPE = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
TEXT_FUNCTIONS = {"Builtin_CONTAINS", "Builtin_REGEX", "Builtin_STRSTARTS", "Builtin_STRENDS"}
STRING_WRAPPERS = {"Builtin_STR", "Builtin_LCASE", "Builtin_UCASE"}


def parse(text: str) -> tuple[Any, Any]:
    try:
        parsed = parseQuery(text)
        return parsed, translateQuery(parsed)
    except Exception as e:  # pyparsing raises its own hierarchy
        raise ParseError(str(e).strip().splitlines()[-1] if str(e).strip() else repr(e)) from e


def analyze(text: str) -> QueryShape:
    parsed, query = parse(text)
    shape = QueryShape()
    body = parsed[1]
    # rdflib's parse tree carries no `projection` key for `SELECT *`
    shape.select_star = (
        isinstance(body, CompValue) and body.name == "SelectQuery" and "projection" not in body
    )
    algebra = query.algebra
    shape.projection = list(getattr(algebra, "PV", None) or [])
    _Walker(shape).walk(algebra, required=True)
    return shape


class _Walker:
    def __init__(self, shape: QueryShape) -> None:
        self.shape = shape
        self._optional_id = 0
        self._union_id = 0
        self._index = 0

    def walk(self, node: Any, *, required: bool, depth: int = 0) -> set[Variable]:
        """Walk one algebra node; return the variables it binds."""
        if not isinstance(node, CompValue):
            return set()
        name = node.name
        s = self.shape
        if name == "BGP":
            bound: set[Variable] = set()
            for s_, p_, o_ in node.get("triples", []):
                tp = TriplePattern(s_, p_, o_, required=required, index=self._index)
                self._index += 1
                s.patterns.append(tp)
                bound |= tp.variables()
                if required:
                    s.nodes.append(Node("pattern", tp.variables(), [tp]))
            return bound
        if name == "TriplesBlock":  # inside SERVICE, untranslated: flattened s p o s p o …
            bound = set()
            for flat in node.get("triples", []):
                for i in range(0, len(flat) - 2, 3):
                    s_, p_, o_ = flat[i], flat[i + 1], flat[i + 2]
                    tp = TriplePattern(s_, p_, o_, required=required, index=self._index)
                    self._index += 1
                    s.patterns.append(tp)
                    bound |= tp.variables()
            return bound
        if name == "Join":
            return self.walk(node["p1"], required=required, depth=depth + 1) | self.walk(
                node["p2"], required=required, depth=depth + 1
            )
        if name == "LeftJoin":
            left = self.walk(node["p1"], required=required, depth=depth + 1)
            start = len(s.patterns)
            self._optional_id += 1
            self.walk(node["p2"], required=False, depth=depth + 1)
            group = s.patterns[start:]
            for tp in group:
                tp.optional_group = self._optional_id
            s.optional_groups.append(group)
            expr = node.get("expr")
            if isinstance(expr, CompValue) and expr.name != "TrueFilter":
                s.filters.append(self._filter(expr, required=False))
            return left
        if name == "Union":
            self._union_id += 1
            uid = self._union_id
            start = len(s.patterns)
            b1 = self.walk(node["p1"], required=False, depth=depth + 1)
            b2 = self.walk(node["p2"], required=False, depth=depth + 1)
            branch = s.patterns[start:]
            for tp in branch:
                tp.union_group = uid
                tp.required = required
            if required:
                s.nodes.append(Node("union", b1 | b2, branch))
            return b1 | b2
        if name == "Minus":
            left = self.walk(node["p1"], required=required, depth=depth + 1)
            self.walk(node["p2"], required=False, depth=depth + 1)
            return left
        if name == "Filter":
            s.filters.append(self._filter(node["expr"], required=required))
            return self.walk(node["p"], required=required, depth=depth + 1)
        if name == "Extend":
            inner = self.walk(node["p"], required=required, depth=depth + 1)
            expr = node.get("expr")
            const = isinstance(expr, (URIRef, Literal))
            if not (isinstance(expr, Variable) and str(expr).startswith("__agg_")):
                s.binds.append((node["var"], expr, const))
            return inner | {node["var"]}
        if name == "ToMultiSet":
            inner = node["p"]
            if isinstance(inner, CompValue) and inner.name == "values":
                rows = inner.get("res", [])
                vars_ = set()
                for row in rows:
                    vars_ |= set(row.keys())
                s.values_vars |= vars_
                if required:
                    s.nodes.append(Node("values", vars_, []))
                return vars_
            # a subselect: its projection is what the outer query sees
            s.subselects += 1
            saved = len(s.patterns)
            self.walk(inner, required=False, depth=depth + 1)
            pv = set(getattr(inner, "PV", None) or [])
            if not pv:
                pv = (
                    set().union(*(p.variables() for p in s.patterns[saved:]))
                    if s.patterns[saved:]
                    else set()
                )
            if required:
                s.nodes.append(Node("subselect", pv, s.patterns[saved:]))
            return pv
        if name in (
            "Project",
            "Distinct",
            "Reduced",
            "OrderBy",
            "Slice",
            "Group",
            "AggregateJoin",
            "Graph",
        ):
            if name == "Distinct":
                s.distinct = True
            elif name == "OrderBy":
                s.order_by = True
            elif name == "Slice":
                s.limit = node.get("length")
            elif name == "Group":
                s.group_by = True
            elif name == "AggregateJoin":
                s.aggregates = True
            return self.walk(node["p"], required=required, depth=depth + 1)
        if name == "ServiceGraphPattern":
            s.services += 1
            start = len(s.patterns)
            bound = self.walk(node.get("graph"), required=required, depth=depth + 1)
            if required:
                s.nodes.append(Node("service", bound, s.patterns[start:]))
            return bound
        if name == "GroupGraphPatternSub":
            bound = set()
            for part in node.get("part", []):
                bound |= self.walk(part, required=required, depth=depth + 1)
            return bound
        # SelectQuery / AskQuery / ConstructQuery / DescribeQuery wrappers, and anything else with a `p`
        if "p" in node:
            return self.walk(node["p"], required=required, depth=depth + 1)
        return set()

    # -- filters ------------------------------------------------------------

    def _filter(self, expr: Any, *, required: bool) -> FilterInfo:
        info = FilterInfo(vars=set(), equalities={}, text=[], required=required, expr=expr)
        self._classify(expr, info)
        return info

    def _classify(self, expr: Any, info: FilterInfo) -> None:
        if isinstance(expr, Variable):
            info.vars.add(expr)
            return
        if isinstance(expr, (list, tuple)):
            for e in expr:
                self._classify(e, info)
            return
        if not isinstance(expr, CompValue):
            return
        name = expr.name
        if name == "RelationalExpression":
            left, op, right = expr.get("expr"), expr.get("op"), expr.get("other")
            if isinstance(left, Variable) and op == "=" and isinstance(right, (URIRef, Literal)):
                info.equalities.setdefault(left, []).append(right)
            elif isinstance(left, Variable) and op == "IN" and isinstance(right, (list, tuple)):
                consts = [r for r in right if isinstance(r, (URIRef, Literal))]
                if consts and len(consts) == len(right):
                    info.equalities.setdefault(left, []).extend(consts)
            self._classify(left, info)
            self._classify(right, info)
            return
        if name in TEXT_FUNCTIONS:
            subject = expr.get("arg1") if name != "Builtin_REGEX" else expr.get("text")
            var, wrapped = _unwrap(subject)
            if var is not None:
                info.text.append((var, name.removeprefix("Builtin_"), wrapped))
            for k in expr:
                if k != "_vars":
                    self._classify(expr[k], info)
            return
        for k in expr:
            if k != "_vars":
                self._classify(expr[k], info)


def _unwrap(expr: Any) -> tuple[Variable | None, bool]:
    """``?v``, ``STR(?v)``, ``LCASE(?v)``, ``LCASE(STR(?v))`` → (?v, wrapped)."""
    wrapped = False
    while isinstance(expr, CompValue) and expr.name in STRING_WRAPPERS:
        wrapped = wrapped or expr.name == "Builtin_STR"
        expr = expr.get("arg")
    if isinstance(expr, Variable):
        return expr, wrapped
    return None, wrapped


def path_is_unbounded(path: Path) -> bool:
    """A ``*`` or ``+`` anywhere in the path."""
    if isinstance(path, MulPath):
        return path.mod in ("*", "+")
    if isinstance(path, (SequencePath, AlternativePath)):
        return any(isinstance(a, Path) and path_is_unbounded(a) for a in path.args)
    if isinstance(path, InvPath):
        return isinstance(path.arg, Path) and path_is_unbounded(path.arg)
    if isinstance(path, NegatedPath):
        return False
    return False


def path_predicates(path: Path | URIRef) -> set[URIRef]:
    """Every IRI a path mentions."""
    if isinstance(path, URIRef):
        return {path}
    if isinstance(path, MulPath):
        return path_predicates(path.path)
    if isinstance(path, InvPath):
        return path_predicates(path.arg)
    if isinstance(path, (SequencePath, AlternativePath)):
        out: set[URIRef] = set()
        for a in path.args:
            out |= path_predicates(a)
        return out
    return set()


def format_term(t: Term) -> str:
    if isinstance(t, Variable):
        return f"?{t}"
    if isinstance(t, URIRef):
        return f"<{t}>"
    if isinstance(t, Literal):
        return t.n3()
    if isinstance(t, Path):
        return t.n3()
    return str(t)
