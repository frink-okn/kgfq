"""Single-pattern fetch planning and execution — the phase-A subset of C4 and
C5: rule R1 (count before fetch) and rule R2 (bind-join versus column-fetch by
a computable rule from the endpoint's own caps), producing an inspectable plan
(D12) before any data request is made.

A fetched table is the pattern's bindings (C6, 2026-11-20): four columns per
variable, none for a constant, which stays in the pattern the receipt carries.
:func:`fetch_variables` names the columns and refuses a pattern that cannot be
given them.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from ..client.endpoint import Endpoint, Pattern, Release
from ..client.errors import BadColumns, TooLarge
from ..client.statistics import Statistics
from ..client.terms import Term, Var, format_term, term_columns, variable_columns

PLAN_SCHEMA = "kgfq.plan/1"
# The guard (C4): a fetch that would read more rows than this is refused unless
# the caller bounds it or asks for all of it. A tool an agent calls has to be
# bounded, and a pattern typed by accident (`? ? ?`) should cost a count.
GUARD_ROWS = 1_000_000
GUARD_ENV = "KGFQ_FETCH_GUARD_ROWS"
# Whole-predicate fetches of 0.5 M to 14 M triples ran at 45,000 to 90,000 rows a
# second (2026-11). Only the refusal's estimate of what was not spent reads this.
MEASURED_ROWS_PER_SECOND = (45_000, 90_000)


def guard_rows() -> int | None:
    """The guard's limit: :data:`GUARD_ROWS`, or ``KGFQ_FETCH_GUARD_ROWS``, where
    ``0`` or ``off`` means no guard."""
    text = os.environ.get(GUARD_ENV, "").strip().lower().replace("_", "").replace(",", "")
    if not text:
        return GUARD_ROWS
    if text in ("0", "off", "none"):
        return None
    if not text.isdigit():
        raise TooLarge(f"{GUARD_ENV}={os.environ[GUARD_ENV]!r} is not a number of rows, or `off`")
    return int(text)


POSITIONS = {"s": "subject", "p": "predicate", "o": "object"}


def fetch_variables(pattern: Pattern) -> list[str]:
    """The term columns of the pattern's fetched table: its variables in
    position order, each once. An anonymous position's column is the position's
    own name, ``s``, ``p`` or ``o``, so ``? ? ?`` fetches the triples under the
    columns a triples table has.

    Refused, since every column needs a name of its own and the name is the
    caller's to choose: a pattern with no variable, whose table would have no
    column; an anonymous position whose name a variable of the pattern already
    has; and two variables whose columns collide (``?c`` and ``?c_kind``)."""
    names = pattern.variables()
    spelled = pattern.spelled()
    if not names:
        raise BadColumns(
            f"`{spelled}` has no variable, so its table would have no column. "
            "`count` says whether the triple is there."
        )
    for pos, v in (("s", pattern.s), ("p", pattern.p), ("o", pattern.o)):
        if v is None and names[pos] != pos:
            holder = next(POSITIONS[q] for q, n in names.items() if n == pos)
            taken = set(names.values())
            name = POSITIONS[pos]
            while name in taken:
                name += "_"
            s, p, o = (
                Var(name) if q == pos else w
                for q, w in (("s", pattern.s), ("p", pattern.p), ("o", pattern.o))
            )
            raise BadColumns(
                f"`{spelled}` leaves its {POSITIONS[pos]} unnamed, and that column would be "
                f"`{pos}`, the name the pattern gives its {holder}. Name the {POSITIONS[pos]}, "
                f"e.g. `{Pattern(s, p, o).spelled()}`."
            )
    variables = list(dict.fromkeys(names.values()))
    seen: dict[str, str] = {}
    for v in variables:
        for column in variable_columns(v):
            if column in seen:
                raise BadColumns(
                    f"`{spelled}`: ?{seen[column]} and ?{v} would both need the column "
                    f"`{column}`. Rename one of them."
                )
            seen[column] = v
    return variables


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


def distinct_rows(rows: Sequence[Sequence[Term]]) -> list[Sequence[Term]]:
    """The input tuples, each once, in the order given. A bindings block joins
    like ``VALUES``, so a tuple given twice would answer twice, and the table
    has no index column to tell the copies apart."""
    seen: dict[tuple[str, ...], Sequence[Term]] = {}
    for r in rows:
        seen.setdefault(tuple(format_term(t) for t in r), r)
    return list(seen.values())


def intent_of(
    pattern: Pattern, vars_: Sequence[str] | None, rows: Sequence[Sequence[Term]] | None
) -> dict[str, Any]:
    intent: dict[str, Any] = {"kind": "fetch", "pattern": pattern.as_body()}
    if rows is not None:
        intent["bindings"] = {
            "vars": list(vars_ or []),
            "rows": len(rows),
            "distinct": len(distinct_rows(rows)),
        }
    return intent


def plan_fetch(
    endpoint: Endpoint,
    release: Release,
    pattern: Pattern,
    *,
    vars_: Sequence[str] | None = None,
    rows: Sequence[Sequence[Term]] | None = None,
    stats: Statistics | None = None,
    guard: int | None = None,
) -> Plan:
    """R1: one `/count` sizes the pattern. R2: with k join keys and pattern
    cardinality c, bind-join costs ⌈k / max_bindings⌉ requests and column-fetch
    ⌈c / max_limit⌉ — derived from the caps this endpoint publishes. Bindings
    that name a variable the pattern does not are refused before R1's count,
    whichever route R2 would take (C1, 2026-11-06), and so is a repeated
    variable with no bindings to carry its equality (2026-11-20).

    With ``guard``, the plan also says how many rows its read would take and
    whether that is over the guard (:func:`check_guard`). A scan reads the
    pattern's count. A bind-join reads at most that, so under the guard it needs
    no second look; over it, one bound `/count` sums what the input tuples
    reach, exactly, at the cost the fetch's own requests would have had."""
    if rows is not None:
        pattern.check_bindings(vars_ or [])
    else:
        pattern.check_names_are_carried()
    fetch_variables(pattern)
    caps = {
        "max_limit": endpoint.cap("max_limit", 10000),
        "max_bindings": endpoint.cap("max_bindings", 200),
    }
    plan = Plan(intent=intent_of(pattern, vars_, rows), steps=[], caps=caps)
    # Under bindings a repeated variable's equality is the server's to apply;
    # the count that sizes the pattern is of the pattern without it, an upper
    # bound. Without bindings the count below refuses it (C1, 2026-11-20).
    repeated = pattern.repeated() if rows is not None else []
    counted = pattern.without_equalities() if repeated else pattern
    if stats is not None:
        count, source = stats.count_source(counted)
    else:
        count, source = endpoint.count(release, counted), "server"
    plan.evidence["count"] = {"value": count.value, "exact": count.exact, "source": source}
    if repeated:
        plan.evidence["count"]["of"] = counted.spelled()
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
        _size(plan, guard)
        return plan
    k = len(distinct_rows(rows))
    bind_requests = max(1, math.ceil(k / caps["max_bindings"]))
    plan.evidence["r2"] = {
        "k": k,
        "c": count.value,
        "bind_join_requests": bind_requests,
        "column_fetch_requests": column_requests,
    }
    if repeated:
        plan.steps.append(
            PlanStep(
                "bindings",
                pattern.as_body(),
                bind_requests,
                None,
                bindings=k,
                note=f"R2: bind-join, since only a body carries the equality on {', '.join('?' + v for v in repeated)}",
            )
        )
    elif bind_requests <= column_requests:
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
    step = plan.steps[-1]
    if guard is not None and step.op == "bindings" and count.value > guard:
        distinct = distinct_rows(rows)
        counts = (
            stats.count_bindings(pattern, vars_ or [], distinct)
            if stats is not None
            else endpoint.count_bindings(release, pattern, vars_ or [], distinct)
        )
        step.estimated_rows = sum(c.value for c in counts)
        plan.evidence["bound_count"] = {
            "value": step.estimated_rows,
            "exact": all(c.exact for c in counts),
        }
    _size(plan, guard)
    return plan


def _size(plan: Plan, guard: int | None) -> None:
    """Record what the plan's read would take against the guard. A bind-join
    with no row estimate is bounded by a pattern count already under it."""
    if guard is None:
        return
    reads = plan.steps[-1].estimated_rows
    plan.evidence["guard"] = {
        "rows": guard,
        "reads": reads,
        "over": reads is not None and reads > guard,
    }


def check_guard(
    plan: Plan,
    pattern: Pattern,
    release: Release,
    *,
    max_rows: int | None = None,
    allow_all: bool = False,
) -> None:
    """Refuse a fetch whose read is over the guard, before a row is read, unless
    the caller bounded it (``max_rows``) or asked for all of it. The refusal
    says what it would have cost and what would make it smaller, since the
    reader is as likely an agent as a person.

    ``max_rows`` bounds a bind-join and a plain scan. It does not bound a scan
    that filters for bound values, which reads the whole pattern to keep a few
    rows, so that one needs ``allow_all``."""
    verdict = plan.evidence.get("guard")
    if not verdict or not verdict["over"] or allow_all:
        return
    step = plan.steps[-1]
    filtered_scan = step.op == "fragment" and step.bindings is not None
    if max_rows is not None and not filtered_scan:
        return
    reads, limit = int(verdict["reads"]), int(verdict["rows"])
    fast, slow = (max(1, round(reads / r)) for r in reversed(MEASURED_ROWS_PER_SECOND))
    took = (
        f"{_duration(fast)} to {_duration(slow)}"
        if _duration(fast) != _duration(slow)
        else _duration(slow)
    )
    what = (
        f"`{pattern.spelled()}` in {release.dataset} matches {reads:,} triples"
        if step.bindings is None
        else f"the {step.bindings:,} bound values reach {reads:,} triples of "
        f"`{pattern.spelled()}` in {release.dataset}"
        if step.op == "bindings"
        else f"filtering for {step.bindings:,} bound values would read all {reads:,} triples of "
        f"`{pattern.spelled()}` in {release.dataset}"
    )
    narrower = (
        "Narrow it: bind a variable to an earlier table's column (`--bind`), or take a part "
        "(`--max-rows N`); `kgfq distinct` lists the values of one position without reading "
        "the triples."
        if step.bindings is None
        else "Bind fewer values, or take a part (`--max-rows N`)."
        if step.op == "bindings"
        else "Bind fewer values, so that a bind-join is the cheaper route."
    )
    raise TooLarge(
        f"{what}: {step.estimated_requests:,} request{'' if step.estimated_requests == 1 else 's'}"
        f", and about {took} at the rates large "
        f"fetches have run at. That is over the guard of {limit:,} rows, so nothing was fetched. "
        f"{narrower} To fetch it all, pass `--all`."
    )


def _duration(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} s"
    return f"{round(seconds / 60)} min"


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
    """Run the plan's data step and return its rows as an Arrow table: four
    columns per variable of the pattern (:func:`fetch_variables`), whichever
    route R2 chose. A bound variable is a column like any other, so the table
    says which input values found something without an index beside it.
    Stopping at ``max_rows`` is said to the read, which marks the response it
    stopped at when the server still had rows; the receipt reads that (C6)."""
    step = plan.steps[-1]
    variables = fetch_variables(pattern)
    out: dict[str, list[str | None]] = {c: [] for v in variables for c in variable_columns(v)}
    taken = 0

    def emit(row: dict[str, Term]) -> None:
        for v in variables:
            for column, cell in zip(variable_columns(v), term_columns(row.get(v)), strict=True):
                out[column].append(cell)

    if rows is not None:
        rows = distinct_rows(rows)
    if step.op == "bindings":
        bound = endpoint.bindings(release, pattern, list(vars_ or []), rows or [])
        for _index, row in bound:
            emit(row)
            taken += 1
            if max_rows is not None and taken >= max_rows:
                bound.stop("max_rows")
                break
    else:
        wanted: set[tuple[str, ...]] | None = None
        if rows is not None:
            wanted = {tuple(format_term(t) for t in r) for r in rows}
        scan = endpoint.fragment(release, pattern)
        for row in scan:
            if wanted is not None:
                got = tuple(format_term(row[v]) if v in row else "" for v in vars_ or [])
                if got not in wanted:
                    continue
            emit(row)
            taken += 1
            if max_rows is not None and taken >= max_rows:
                scan.stop("max_rows")
                break
    return pa.table({c: pa.array(cells, type=pa.string()) for c, cells in out.items()})
