"""The twelve rules (plan §5.6), from the client-suite report §4, over a
:class:`QueryShape`. A rule never rewrites; it names what the syntax hides and
says which kgfq command or authoring move makes the selectivity explicit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from rdflib import URIRef, Variable

from .analysis import (
    LABEL_PREDICATES,
    RDF_TYPE,
    Node,
    QueryShape,
    format_term,
    path_is_unbounded,
    path_predicates,
)

SEVERITIES = ("info", "warning", "strong")


@dataclass(slots=True)
class Diagnostic:
    code: str
    severity: str
    title: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


RULES: dict[str, tuple[str, str]] = {
    "KGFQ001": ("warning", "finite equality or IN filter could be VALUES"),
    "KGFQ002": ("warning", "constant BIND used as a remote selection constraint"),
    "KGFQ003": ("warning", "variable predicate restricted only by namespace or string filtering"),
    "KGFQ004": ("strong", "required pattern has disconnected components: a Cartesian product"),
    "KGFQ005": ("strong", "an OPTIONAL is the only apparent bridge between required components"),
    "KGFQ006": ("warning", "broad label predicate followed by an unanchored text filter"),
    "KGFQ007": ("info", "LIMIT follows a blocking DISTINCT, GROUP BY, or ORDER BY"),
    "KGFQ008": ("info", "query exceeds the complex-pattern review threshold"),
    "KGFQ009": ("warning", "aggregation follows multiple multi-valued joins or optionals"),
    "KGFQ010": ("warning", "unbounded property path lacks a selective endpoint"),
    "KGFQ011": ("info", "SELECT * projects variables the caller does not need"),
    "KGFQ012": ("warning", "transitive path over a predicate the dataset declares already closed"),
}


def _diag(
    code: str, message: str, detail: dict[str, Any] | None = None, suggestion: str | None = None
) -> Diagnostic:
    severity, title = RULES[code]
    return Diagnostic(code, severity, title, message, detail or {}, suggestion)


class _Components:
    """Union-find over variables; required nodes are joined through the
    variables they share, filters and binds through the variables they mention."""

    def __init__(self, shape: QueryShape) -> None:
        self.parent: dict[Any, Any] = {}
        self.node_key: dict[int, Any] = {}
        for i, node in enumerate(shape.nodes):
            key = ("node", i)
            self.parent.setdefault(key, key)
            for v in node.vars:
                self.union(key, v)
        for f in shape.filters:
            if f.required and len(f.vars) > 1:
                first, *rest = sorted(f.vars, key=str)
                for v in rest:
                    self.union(first, v)
        for var, expr, const in shape.binds:
            if not const:
                for v in _expr_vars(expr):
                    self.union(var, v)

    def find(self, x: Any) -> Any:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def of_node(self, i: int) -> Any:
        return self.find(("node", i))

    def of_var(self, v: Variable) -> Any | None:
        return self.find(v) if v in self.parent else None


def _expr_vars(expr: Any) -> set[Variable]:
    from rdflib.plugins.sparql.parserutils import CompValue

    if isinstance(expr, Variable):
        return {expr}
    if isinstance(expr, CompValue):
        out: set[Variable] = set()
        for k in expr:
            if k != "_vars":
                out |= _expr_vars(expr[k])
        return out
    if isinstance(expr, (list, tuple)):
        out = set()
        for e in expr:
            out |= _expr_vars(e)
        return out
    return set()


def lint_shape(
    shape: QueryShape,
    *,
    threshold: int = 6,
    closed_predicates: set[str] | None = None,
    label_predicates: set[str] | None = None,
) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    closed = {URIRef(p) for p in (closed_predicates or ())}
    labels = {URIRef(p) for p in label_predicates} if label_predicates else LABEL_PREDICATES
    bound = shape.bound_vars()
    required = shape.required_patterns

    # KGFQ001 — finite equality / IN on a definitely bound variable
    for f in shape.filters:
        if not f.required:
            continue
        for var, consts in f.equalities.items():
            if var not in bound:
                continue
            values = " ".join(dict.fromkeys(format_term(c) for c in consts))
            if len(consts) == 1:
                out.append(
                    _diag(
                        "KGFQ001",
                        f"FILTER(?{var} = {values}) hides a constant the pattern could carry; the engine sees a scan and a filter where one bound pattern would do",
                        {"variable": f"?{var}", "values": [format_term(c) for c in consts]},
                        f"put {values} directly in the triple pattern in place of ?{var}, or `VALUES ?{var} {{ {values} }}`",
                    )
                )
            else:
                out.append(
                    _diag(
                        "KGFQ001",
                        f"a finite set of {len(dict.fromkeys(consts))} values for ?{var} is expressed as a FILTER; source selection cannot see it",
                        {"variable": f"?{var}", "values": [format_term(c) for c in consts]},
                        f"VALUES ?{var} {{ {values} }} before the pattern that binds ?{var}",
                    )
                )

    # KGFQ002 — constant BIND feeding a required pattern
    pattern_vars = set().union(*(p.variables() for p in required)) if required else set()
    for var, expr, const in shape.binds:
        if const and var in pattern_vars:
            out.append(
                _diag(
                    "KGFQ002",
                    f"BIND({format_term(expr)} AS ?{var}) is a constant used to restrict a remote pattern; engines evaluate BIND after the pattern, so nothing is pushed down",
                    {"variable": f"?{var}", "value": format_term(expr)},
                    f"VALUES ?{var} {{ {format_term(expr)} }}, or the constant in the pattern itself",
                )
            )

    # KGFQ003 — variable predicate restricted only by a string filter
    for p in required:
        if not isinstance(p.p, Variable) or p.p in shape.values_vars:
            continue
        for f in shape.filters:
            hits = [(v, fn, wrapped) for v, fn, wrapped in f.text if v == p.p]
            if hits:
                fn = hits[0][1]
                out.append(
                    _diag(
                        "KGFQ003",
                        f"?{p.p} is a variable predicate constrained only by {fn}; the pattern is an unbounded scan of every triple (the NCIPID case read 597,129 triples for 67,267 answers)",
                        {"variable": f"?{p.p}", "function": fn},
                        f"read the predicate inventory (`kgfq schema <ds> --children properties --all`), pick the matching IRIs, and write `VALUES ?{p.p} {{ … }}`",
                    )
                )
                break

    # KGFQ004 / KGFQ005 — connectivity of the required pattern
    if len(shape.nodes) > 1:
        comps = _Components(shape)
        roots = {comps.of_node(i) for i in range(len(shape.nodes))}
        if len(roots) > 1:
            groups: dict[Any, list[Node]] = {}
            for i, node in enumerate(shape.nodes):
                groups.setdefault(comps.of_node(i), []).append(node)
            described = [
                sorted({f"?{v}" for n in nodes for v in n.vars}) for nodes in groups.values()
            ]
            # a component whose every pattern has a constant subject is a lookup
            # of one entity's values: still a product, but a small one
            anchored = [
                all(
                    not isinstance(tp.s, Variable) and not tp.path()
                    for n in nodes
                    for tp in n.patterns
                )
                and all(n.patterns for n in nodes)
                for nodes in groups.values()
            ]
            small = sum(anchored) >= len(groups) - 1
            diag = _diag(
                "KGFQ004",
                f"the required pattern has {len(roots)} components that share no variable; the query denotes their Cartesian product"
                + (
                    " — the extra component is a constant-subject lookup, so the product is small, but say so if it is meant"
                    if small
                    else ""
                ),
                {"components": described, "small": small},
                "connect every required component through a shared variable, or split the query; a product is almost never the intent",
            )
            if small:
                diag.severity = "warning"
            out.append(diag)
            for group in shape.optional_groups:
                touched = {comps.of_var(v) for tp in group for v in tp.variables()}
                touched.discard(None)
                if len(touched) > 1:
                    out.append(
                        _diag(
                            "KGFQ005",
                            "an OPTIONAL is the only pattern relating two required components; OPTIONAL neither connects nor constrains them",
                            {
                                "optional": [
                                    f"{format_term(tp.s)} {format_term(tp.p)} {format_term(tp.o)}"
                                    for tp in group
                                ]
                            },
                            "if the relationship is required, make the OPTIONAL pattern mandatory",
                        )
                    )
                    break

    # KGFQ006 — broad label predicate + text filter as the primary lookup
    for p in required:
        if not (isinstance(p.p, URIRef) and p.p in labels and isinstance(p.o, Variable)):
            continue
        if not isinstance(p.s, Variable) or shape.constant_bound(p.s):
            continue
        for f in shape.filters:
            hits = [(v, fn, w) for v, fn, w in f.text if v == p.o]
            if hits:
                fn = hits[0][1]
                out.append(
                    _diag(
                        "KGFQ006",
                        f"{fn} over ?{p.o} from {format_term(p.p)} is the primary lookup for ?{p.s}; that scans every label (3.53 M in the ProKN case) where a text index would return the candidates",
                        {
                            "subject": f"?{p.s}",
                            "label": f"?{p.o}",
                            "predicate": format_term(p.p),
                            "function": fn,
                        },
                        f"if the literal is known, bind it exactly; otherwise `kgfq resolve <text>` (/search) for candidate IRIs, then `VALUES ?{p.s} {{ … }}` and keep the FILTER as the residual check",
                    )
                )
                break

    # KGFQ007 — LIMIT after a blocking operator
    if shape.limit is not None and (shape.distinct or shape.group_by or shape.order_by):
        blocking = [
            n
            for n, on in (
                ("DISTINCT", shape.distinct),
                ("GROUP BY", shape.group_by),
                ("ORDER BY", shape.order_by),
            )
            if on
        ]
        out.append(
            _diag(
                "KGFQ007",
                f"LIMIT {shape.limit} follows {' and '.join(blocking)}; the engine must consume the whole input before it can return the first row (SCALES: 1.68 M triples for 75 values)",
                {"limit": shape.limit, "blocking": blocking},
                "if a bounded arbitrary sample is the intent, say so with an inner bounded stage; if the distinct values are the intent, expect the full scan and fetch to a table instead",
            )
        )

    # KGFQ008 — review threshold
    total = len(shape.patterns)
    if total > threshold:
        out.append(
            _diag(
                "KGFQ008",
                f"{total} triple patterns exceed the review threshold of {threshold}; above six patterns the measured completion rate over fragments fell from 36/38 to 6/15",
                {"patterns": total, "threshold": threshold},
                "decompose into a selective IRI-producing stage and VALUES-based extension or hydration",
            )
        )

    # KGFQ009 — aggregation over multi-valued joins
    if shape.aggregates or shape.group_by:
        multi = [
            p for p in required if isinstance(p.o, Variable) and p.p != RDF_TYPE and not p.path()
        ]
        if len(multi) + len(shape.optional_groups) >= 2:
            out.append(
                _diag(
                    "KGFQ009",
                    f"aggregation over {len(multi)} potentially multi-valued patterns and {len(shape.optional_groups)} optionals; solution mappings multiply before they are counted, and the whole input must be read",
                    {
                        "multi_valued": [
                            f"{format_term(p.s)} {format_term(p.p)} {format_term(p.o)}"
                            for p in multi
                        ],
                        "optionals": len(shape.optional_groups),
                    },
                    "establish which relations are one-to-one, narrow the required pattern before GROUP BY, and aggregate before joining optional many-valued properties — or fetch the relations to tables and aggregate in SQL",
                )
            )

    # KGFQ010 / KGFQ012 — property paths
    for p in shape.patterns:
        path = p.path()
        if path is None or not path_is_unbounded(path):
            continue
        preds = path_predicates(path)
        closed_hit = preds & closed
        if closed_hit:
            out.append(
                _diag(
                    "KGFQ012",
                    f"{format_term(path)} expands a predicate this dataset already publishes transitively closed; every ancestor is a direct triple",
                    {"path": format_term(path), "closed": sorted(str(c) for c in closed_hit)},
                    f"query {' / '.join(format_term(c) for c in closed_hit)} directly, without * or +",
                )
            )
            continue
        s_anchored = not isinstance(p.s, Variable) or shape.constant_bound(p.s)
        o_anchored = not isinstance(p.o, Variable) or shape.constant_bound(p.o)
        if not (s_anchored or o_anchored):
            out.append(
                _diag(
                    "KGFQ010",
                    f"{format_term(path)} has no selective endpoint: neither {format_term(p.s)} nor {format_term(p.o)} is bound to a constant, so the traversal starts everywhere",
                    {
                        "path": format_term(path),
                        "subject": format_term(p.s),
                        "object": format_term(p.o),
                    },
                    "bind one endpoint, use a fixed-length relation, or a published closure predicate",
                )
            )

    # KGFQ011 — SELECT *
    if shape.select_star:
        out.append(
            _diag(
                "KGFQ011",
                "SELECT * projects every variable; extra variables raise transfer, grouping and distinctness cost and expose unintended multiplicity",
                {},
                "name the variables the caller needs",
            )
        )

    return sorted(out, key=lambda d: (SEVERITIES.index(d.severity) * -1, d.code))
