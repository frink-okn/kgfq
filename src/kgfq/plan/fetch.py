"""Single-pattern fetch planning and execution — the phase-A subset of C4 and
C5: rule R1 (count before fetch) and rule R2 (bind-join versus column-fetch by
a computable rule from the endpoint's own caps), producing an inspectable plan
(D12) before any data request is made.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from ..client.endpoint import Endpoint, Pattern, Release
from ..client.statistics import Statistics
from ..client.terms import Term, format_term, term_columns

PLAN_SCHEMA = "kgfq.plan/1"
TABLE_COLUMNS = ["s", "p", "o", "o_lang", "o_datatype"]


@dataclass(slots=True)
class PlanStep:
    op: str
    pattern: dict[str, str]
    estimated_requests: int
    estimated_rows: int | None
    note: str = ""
    bindings: int | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "op": self.op,
            "pattern": self.pattern,
            "estimated_requests": self.estimated_requests,
            "estimated_rows": self.estimated_rows,
            "note": self.note,
        }
        if self.bindings is not None:
            d["bindings"] = self.bindings
        return d


@dataclass(slots=True)
class Plan:
    intent: dict[str, Any]
    steps: list[PlanStep]
    evidence: dict[str, Any] = field(default_factory=dict)
    approximations: list[str] = field(default_factory=list)
    caps: dict[str, int] = field(default_factory=dict)

    @property
    def estimated_requests(self) -> int:
        return sum(s.estimated_requests for s in self.steps)

    @property
    def estimated_rows(self) -> int | None:
        rows = [s.estimated_rows for s in self.steps if s.op in ("fragment", "bindings")]
        return sum(r for r in rows if r is not None) if rows else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "intent": self.intent,
            "caps": self.caps,
            "evidence": self.evidence,
            "steps": [s.as_dict() for s in self.steps],
            "estimated_requests": self.estimated_requests,
            "estimated_rows": self.estimated_rows,
            "approximations": self.approximations,
            "completeness_expectation": "complete",
        }


def intent_of(
    pattern: Pattern, vars_: Sequence[str] | None, rows: Sequence[Sequence[Term]] | None
) -> dict[str, Any]:
    intent: dict[str, Any] = {"kind": "fetch", "pattern": pattern.as_body()}
    if rows is not None:
        intent["bindings"] = {"vars": list(vars_ or []), "rows": len(rows)}
    return intent


def plan_fetch(
    endpoint: Endpoint,
    release: Release,
    pattern: Pattern,
    *,
    vars_: Sequence[str] | None = None,
    rows: Sequence[Sequence[Term]] | None = None,
    stats: Statistics | None = None,
) -> Plan:
    """R1: one `/count` sizes the pattern. R2: with k join keys and pattern
    cardinality c, bind-join costs ⌈k / max_bindings⌉ requests and column-fetch
    ⌈c / max_limit⌉ — derived from the caps this endpoint publishes."""
    caps = {
        "max_limit": endpoint.cap("max_limit", 10000),
        "max_bindings": endpoint.cap("max_bindings", 200),
    }
    plan = Plan(intent=intent_of(pattern, vars_, rows), steps=[], caps=caps)
    if stats is not None:
        count, source = stats.count_source(pattern)
    else:
        count, source = endpoint.count(release, pattern), "server"
    plan.evidence["count"] = {"value": count.value, "exact": count.exact, "source": source}
    column_requests = max(1, math.ceil(count.value / caps["max_limit"]))
    if rows is None:
        plan.steps.append(
            PlanStep(
                "fragment",
                pattern.as_body(),
                column_requests,
                count.value,
                note="R1: paged at max_limit",
            )
        )
        return plan
    k = len(rows)
    bind_requests = max(1, math.ceil(k / caps["max_bindings"]))
    plan.evidence["r2"] = {
        "k": k,
        "c": count.value,
        "bind_join_requests": bind_requests,
        "column_fetch_requests": column_requests,
    }
    if bind_requests <= column_requests:
        plan.steps.append(
            PlanStep(
                "bindings",
                pattern.as_body(),
                bind_requests,
                None,
                bindings=k,
                note=f"R2: bind-join, ⌈{k}/{caps['max_bindings']}⌉ ≤ ⌈{count.value}/{caps['max_limit']}⌉",
            )
        )
    else:
        plan.steps.append(
            PlanStep(
                "fragment",
                pattern.as_body(),
                column_requests,
                count.value,
                bindings=k,
                note=f"R2: column-fetch then filter locally, ⌈{count.value}/{caps['max_limit']}⌉ < ⌈{k}/{caps['max_bindings']}⌉",
            )
        )
    return plan


def execute_fetch(
    endpoint: Endpoint,
    release: Release,
    pattern: Pattern,
    plan: Plan,
    *,
    vars_: Sequence[str] | None = None,
    rows: Sequence[Sequence[Term]] | None = None,
    max_rows: int | None = None,
) -> pa.Table:
    """Run the plan's data step and return the triples as an Arrow table with
    the doc 06 §6.3 columns, plus ``binding`` when bindings were supplied."""
    step = plan.steps[-1]
    names = pattern.variables()
    const = {pos: t for pos, t in pattern.bound().items()}
    out: dict[str, list[Any]] = {c: [] for c in TABLE_COLUMNS}
    binding_col: list[int] = []

    def emit(row: dict[str, Term], binding: int | None) -> None:
        s = row.get(names.get("s", "")) if "s" in names else const.get("s")
        p = row.get(names.get("p", "")) if "p" in names else const.get("p")
        o = row.get(names.get("o", "")) if "o" in names else const.get("o")
        out["s"].append(term_columns(s)[0])
        out["p"].append(term_columns(p)[0])
        ov, ol, od = term_columns(o)
        out["o"].append(ov)
        out["o_lang"].append(ol)
        out["o_datatype"].append(od)
        if binding is not None:
            binding_col.append(binding)

    if step.op == "bindings":
        for index, row in endpoint.bindings(release, pattern, list(vars_ or []), rows or []):
            emit(row, index)
            if max_rows is not None and len(out["s"]) >= max_rows:
                break
    else:
        wanted: dict[str, dict[str, int]] | None = None
        if rows is not None:
            wanted = {}
            for i, r in enumerate(rows):
                for v, t in zip(vars_ or [], r, strict=True):
                    wanted.setdefault(v, {}).setdefault(format_term(t), i)
        for row in endpoint.fragment(release, pattern, max_rows=None if wanted else max_rows):
            binding = None
            if wanted:
                hit = True
                for v, table in wanted.items():
                    val = row.get(v)
                    if val is None or format_term(val) not in table:
                        hit = False
                        break
                    binding = table[format_term(val)]
                if not hit:
                    continue
            emit(row, binding if wanted else None)
            if max_rows is not None and len(out["s"]) >= max_rows:
                break
    arrays = {c: pa.array(out[c], type=pa.string()) for c in TABLE_COLUMNS}
    if rows is not None:
        arrays["binding"] = pa.array(binding_col, type=pa.int64())
    return pa.table(arrays)
