"""The operations, one function each, which the skins call (plan §0, D3).

An operation resolves its graph, logs its step in the registry's spelling
(``docs/session/operations.yaml``), asks the server, files its table with a
receipt, and returns a handle: the table's name and what a caller needs to
decide what to do next, never the rows as a matter of course. ``kgfq.cli`` and
``kgfq.mcp`` parse arguments and print; nothing about an operation is decided
in either.

The operations ``../kgf-explorer`` has and kgfq lacked land here
(``notes/session-format.md`` §8). The ones written before it (``fetch``,
``count``, ``schema``…) are still methods of the CLI and move here with
``kgfq-mcp``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from .client.endpoint import Endpoint, Pattern, Release
from .client.errors import CapabilityUnavailable, KgfqError
from .client.session import Session
from .client.statistics import Statistics
from .client.terms import (
    BadTermSyntax,
    Iri,
    Literal,
    Term,
    Var,
    format_term,
    parse_term,
    term_columns,
    term_from_columns,
    term_from_json,
    variable_columns,
)
from .plan import check_guard, execute_fetch, guard_rows, plan_fetch
from .workspace import sandbox
from .workspace.bind import bind_rows, unmatched_report
from .workspace.log import Step, weakest_grade
from .workspace.receipts import completeness, new_receipt, request_entries, source_of, totals_of
from .workspace.store import Workspace, WorkspaceError

SEARCH_LIMIT = 20
SEARCH_VARIABLES = ["subject", "label", "predicate", "matched", "match_kind", "score"]
LABEL_VARIABLES = ["iri", "label", "graph"]
DEFAULT_PREFIXES = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}
RDF_TYPE = Iri("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
TEMPLATE_LIMIT = 200
PREVIEW_ROWS = 20


@dataclass
class Context:
    """Who is running operations: the workspace they write to, the session of
    graphs they read, the surface that was driven and who asked (C9)."""

    workspace: Workspace
    session: Session
    surface: str = "library"
    actor: str | None = None
    version: str | None = None

    def resolve(self, graph: str) -> tuple[Endpoint, Release]:
        return self.session.resolve(graph, version=self.version)

    def step(self, operation: str, **args: Any) -> Step:
        return Step(
            self.workspace,
            self.surface,
            operation,
            {k: v for k, v in args.items() if v is not None},
            actor=self.actor,
        )


def pin_of(release: Release) -> dict[str, Any]:
    return {
        "origin": release.origin,
        "dataset": release.dataset,
        "version": release.version,
        "content_digest": release.content_digest,
        "dataset_iri": release.dataset_iri,
    }


def bindings_table(variables: list[str], rows: list[dict[str, Term | None]]) -> pa.Table:
    """Rows of terms as a ``bindings`` table: four columns per variable (C6)."""
    columns: dict[str, list[str | None]] = {c: [] for v in variables for c in variable_columns(v)}
    for row in rows:
        for v in variables:
            for column, cell in zip(variable_columns(v), term_columns(row.get(v)), strict=True):
                columns[column].append(cell)
    return pa.table({c: pa.array(cells, type=pa.string()) for c, cells in columns.items()})


def table_name(prefix: str, text: str) -> str:
    """A table name from an operation and what it was asked: ``search_atrazine``."""
    slug = "_".join("".join(ch if ch.isalnum() else " " for ch in text).split())[:40]
    return f"{prefix}_{slug}" if slug else prefix


def search(
    ctx: Context, graph: str, text: str, *, limit: int = SEARCH_LIMIT, out: str | None = None
) -> dict[str, Any]:
    """``search``: full-text search over one graph's literals, labels first
    (doc 03 §3.4.5). How a name becomes an IRI, and so the first step of most
    questions. The hits are a table like any other: a later step binds from its
    ``subject`` column, and an IRI taken from it is derived, not asserted (C9).

    The server ranks, and a hit's ``match_kind`` says how it matched; scores
    are not comparable across graphs (C7). One request: the server answers with
    its top ``limit`` hits, at most its ``max_search_results``, and no cursor.
    A table that holds as many hits as were asked for is the first so many by
    rank and says so, since there may be more."""
    text = text.strip()
    if not text:
        raise KgfqError("search needs words to find")
    if limit < 1:
        raise KgfqError(f"--limit is a number of hits, at least 1, not {limit}")
    ep, rel = ctx.resolve(graph)
    if "search" not in rel.links:
        raise CapabilityUnavailable(
            f"{rel.dataset}@{rel.version} on {ep.origin} publishes no `search` link: its bundle "
            "was built without a text index"
        )
    name = ctx.workspace.check_name(out or table_name("search", text))
    step = ctx.step("search", graph=graph, text=text, limit=limit, out=name)
    step.pin(pin_of(rel))
    # /search answers with its top `limit` hits and no cursor, up to the server's
    # own cap: a ranking has a head, not pages (checked on kgf 0.6.0).
    asked = min(limit, ep.cap("max_search_results", 1000))
    with ep.trace() as t:
        page = ep.search(rel, text, limit=asked)
        hits: list[dict[str, Any]] = list((page.body or {}).get("results", []))
        page.record.rows = len(hits)
    rows: list[dict[str, Term | None]] = []
    for hit in hits:
        match = hit.get("match") or {}
        rows.append(
            {
                "subject": term_from_json(hit["subject"]),
                "label": None if hit.get("label") is None else Literal(str(hit["label"])),
                "predicate": Iri(match["predicate"]) if match.get("predicate") else None,
                "matched": None if match.get("literal") is None else Literal(str(match["literal"])),
                "match_kind": Literal(str(hit["match_kind"])) if hit.get("match_kind") else None,
                "score": None if hit.get("score") is None else Literal(str(hit["score"])),
            }
        )
    table = bindings_table(SEARCH_VARIABLES, rows)
    limit_reached = len(hits) >= asked
    approximations = []
    if limit_reached:
        approximations.append(
            f"the first {asked} hits by the server's rank; there may be more"
            + (
                " (--limit)"
                if asked == limit
                else f", and this server gives a search at most {asked} (max_search_results)"
            )
        )
    receipt = new_receipt(
        kind="search",
        step=step.step,
        grade="bundle",
        sources=[source_of(rel)],
        shape="bindings",
        requests=request_entries(t.records),
        totals=totals_of(t.records),
        completeness=completeness(t.records),
        approximations=approximations,
        detail={"text": text, "limit": limit},
    )
    info = ctx.workspace.write_table(name, table, receipt)
    step.receipt(info.receipt)
    step.table(info.receipt)
    step.finish()
    return {
        "table": info.name,
        "path": str(info.path),
        "rows": info.rows,
        "columns": SEARCH_VARIABLES,
        "grade": "bundle",
        "limit_reached": limit_reached,
        "approximations": approximations,
        "requests": len(t.records),
        "step": step.step,
        "content_hash": info.content_hash,
        "hits": [
            {
                "subject": hit["subject"]["value"],
                "label": hit.get("label"),
                "matched": (hit.get("match") or {}).get("literal"),
                "predicate": (hit.get("match") or {}).get("predicate"),
                "match_kind": hit.get("match_kind"),
            }
            for hit in hits
        ],
    }


def graphs_of(receipt: dict[str, Any]) -> list[str]:
    """The graphs a table's values came from, each as ``id@origin``: the ones
    it was read from, or for a table SQL derived, the ones its inputs were."""
    read = [
        f"{s['dataset']}@{s['origin']}"
        for s in receipt["core"].get("sources", [])
        if s.get("kind") == "kgf"
    ]
    return read or list(receipt.get("detail", {}).get("graphs", []))


def labels(
    ctx: Context,
    table: str,
    column: str,
    *,
    graphs: list[str] | None = None,
    out: str | None = None,
) -> dict[str, Any]:
    """``labels``: one label per IRI in a column of an earlier table, as a new
    table to join, for when names have to be part of the data (doc 03's
    ``/labels``; ``../kgf-explorer``'s operation of the name).

    Each graph picks a label by its own declared label predicates. The graphs
    are asked in order and the first label found wins; the default is the
    graphs the table was read from, which is often not where the names are, so
    an IRI no graph labels is a row with no label, and the handle counts them
    with a sample. Label declarations are wrong on a share of bundles (C7): a
    missing label is a fact about the declaration as often as about the node."""
    ws = ctx.workspace
    _, rows = bind_rows(ws, {"table": table, "columns": {"iri": column}})
    iris = [r[0].value for r in rows if isinstance(r[0], Iri)]
    if not iris:
        raise WorkspaceError(
            f"table {table}'s column {column!r} holds no IRI to label"
            + (f": its {len(rows)} values are literals or blank nodes" if rows else "")
        )
    asked = list(graphs or []) or graphs_of(ws.read_receipt(table))
    if not asked:
        raise WorkspaceError(
            f"table {table} was read from no graph, so name the graphs to ask for labels (--graph)"
        )
    name = ws.check_name(out or table_name("labels", f"{table} {column}"))
    resolved = []
    for graph in asked:
        ep, rel = ctx.resolve(graph)
        if "labels" not in rel.links:
            raise CapabilityUnavailable(
                f"{rel.dataset}@{rel.version} on {ep.origin} publishes no `labels` link"
            )
        resolved.append((ep, rel))
    step = ctx.step("labels", table=table, column=column, graphs=graphs, out=name)
    step.input_table(table)
    found: dict[str, tuple[str, str]] = {}
    records = []
    for ep, rel in resolved:
        step.pin(pin_of(rel))
        remaining = [i for i in iris if i not in found]
        if not remaining:
            continue
        with ep.trace() as t:
            answer = ep.labels(rel, remaining)
        records.extend(t.records)
        for iri, label in answer.items():
            if label is not None:
                found[iri] = (str(label), rel.dataset)
    result = bindings_table(
        LABEL_VARIABLES,
        [
            {
                "iri": Iri(i),
                "label": Literal(found[i][0]) if i in found else None,
                "graph": Literal(found[i][1]) if i in found else None,
            }
            for i in iris
        ],
    )
    report = unmatched_report(
        ["iri"],
        [[Iri(i)] for i in iris],
        {(format_term(Iri(i)),) for i in found},
        complete=True,
        bind={"table": table, "columns": {"iri": column}},
    )
    receipt = new_receipt(
        kind="labels",
        step=step.step,
        grade="bundle",
        sources=[source_of(rel) for _, rel in resolved],
        shape="bindings",
        requests=request_entries(records),
        totals=totals_of(records),
        completeness=completeness(records),
        inputs={table: step.record["inputs"][table]},
        bindings=report,
        detail={"column": column, "not_iris": len(rows) - len(iris)},
    )
    info = ws.write_table(name, result, receipt)
    step.receipt(info.receipt)
    step.table(info.receipt)
    step.finish()
    return {
        "table": info.name,
        "path": str(info.path),
        "rows": info.rows,
        "columns": LABEL_VARIABLES,
        "grade": "bundle",
        "graphs": [rel.dataset for _, rel in resolved],
        "labeled": report["matched"],
        "unlabeled": report["unmatched"],
        "unlabeled_sample": [t[0] for t in report["unmatched_sample"]],
        "not_iris": len(rows) - len(iris),
        "requests": len(records),
        "step": step.step,
        "content_hash": info.content_hash,
    }


# The one-pattern templates (``../kgf-explorer`` D2: `describe`, `incoming`,
# `instances`, `neighbors`). There they are SPARQL the page wrote, run by an
# engine in the tab. Each is a single triple pattern, so here each is a fetch
# the plan sizes and the guard bounds (D3): no engine, and no SPARQL at all.
TEMPLATES: dict[str, tuple[str, ...]] = {
    "describe": ("iri",),
    "incoming": ("iri",),
    "instances": ("class",),
    "neighbors": ("iri", "predicate"),
}


def template_pattern(operation: str, terms: dict[str, Iri], direction: str = "out") -> Pattern:
    """The pattern a template stands for, under the explorer's variable names."""
    if operation == "describe":
        return Pattern(terms["iri"], Var("p"), Var("o"))
    if operation == "incoming":
        return Pattern(Var("s"), Var("p"), terms["iri"])
    if operation == "instances":
        return Pattern(Var("s"), RDF_TYPE, terms["class"])
    if operation == "neighbors":
        if direction not in ("out", "in"):
            raise KgfqError(f'direction is "out" or "in", not {direction!r}')
        if direction == "out":
            return Pattern(terms["iri"], terms["predicate"], Var("node"))
        return Pattern(Var("node"), terms["predicate"], terms["iri"])
    raise KgfqError(f"no template named {operation!r}; there are {', '.join(TEMPLATES)}")


def iri_argument(text: str, name: str, prefixes: dict[str, str]) -> Iri:
    """An IRI given as an argument: bare and absolute as it appears in a table,
    bracketed, or a CURIE against the graph's prefixes."""
    text = text.strip()
    if text.startswith(("http://", "https://")):
        return Iri(text)
    term = parse_term(text, prefixes)
    if not isinstance(term, Iri):
        raise BadTermSyntax(f"{name} is an IRI, not {text!r}")
    return term


NUMERIC = frozenset(
    f"http://www.w3.org/2001/XMLSchema#{t}"
    for t in ("integer", "int", "long", "short", "decimal", "double", "float")
)
SAFE_INTEGER = 2**53 - 1


def compact(term: Term | None) -> str | int | float | bool | None:
    """A term for a preview, as ``../kgf-explorer`` shows one to a model: an IRI
    bare, as a later argument takes it; a number as a number, where it is one a
    reader of JSON can hold exactly; a literal in request syntax only where a
    tag or a datatype says something more."""
    if term is None:
        return None
    if isinstance(term, Iri):
        return term.value
    if isinstance(term, Literal) and not term.lang:
        if term.datatype is None:
            return term.value
        if term.datatype in NUMERIC:
            try:
                number = float(term.value)
            except ValueError:
                return format_term(term)
            if math.isfinite(number) and abs(number) <= SAFE_INTEGER:
                whole = term.datatype.endswith(("integer", "int", "long", "short"))
                return int(term.value) if whole else number
        if term.datatype.endswith("#boolean") and term.value in ("true", "false"):
            return term.value == "true"
    return format_term(term)


def preview(
    table: pa.Table, variables: list[str], rows: int = PREVIEW_ROWS
) -> list[dict[str, Any]]:
    """The first rows of a ``bindings`` table, compact, for a handle."""
    head = table.slice(0, rows).to_pylist()
    return [
        {
            v: None
            if r[v] is None
            else compact(
                term_from_columns(r[v], r[f"{v}_kind"], r[f"{v}_lang"], r[f"{v}_datatype"])
            )
            for v in variables
        }
        for r in head
    ]


def template(
    ctx: Context,
    operation: str,
    graphs: list[str],
    arguments: dict[str, str],
    *,
    direction: str | None = None,
    limit: int = TEMPLATE_LIMIT,
    out: str | None = None,
) -> dict[str, Any]:
    """``describe``, ``incoming``, ``instances`` or ``neighbors``: a look at one
    thing, across one or more graphs, as a table whose columns are the
    explorer's (``p, o``; ``s, p``; ``s``; ``node``).

    Each graph is asked the same pattern, in order, and the rows are the bag of
    what they answer, as a federated SELECT's are: a triple two graphs hold is
    two rows. ``limit`` bounds the whole read, so the fetch guard has nothing to
    refuse; a table that reached it says so. The receipt's ``pattern`` is the
    one every row answers, and its detail says how many rows each graph gave,
    in order, which is how a row is traced to its graph."""
    if not graphs:
        raise KgfqError(f"{operation} needs at least one graph")
    if limit < 1:
        raise KgfqError(f"--limit is a number of rows, at least 1, not {limit}")
    resolved = [ctx.resolve(g) for g in graphs]
    ep0, rel0 = resolved[0]
    prefixes = {**DEFAULT_PREFIXES, **ep0.prefixes(rel0)}
    terms = {k: iri_argument(arguments[k], k, prefixes) for k in TEMPLATES[operation]}
    pattern = template_pattern(operation, terms, direction or "out")
    variables = list(dict.fromkeys(pattern.variables().values()))
    subject = next(iter(terms.values())).value
    tail = subject.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    name = ctx.workspace.check_name(out or table_name(operation, tail))
    step = ctx.step(
        operation,
        graphs=graphs,
        **{k: t.value for k, t in terms.items()},
        direction=direction,
        limit=limit,
        out=name,
    )
    inputs = step.input_values([t.value for t in terms.values()])
    tables: list[pa.Table] = []
    records = []
    plans = []
    by_graph: dict[str, int] = {}
    taken = 0
    for ep, rel in resolved:
        step.pin(pin_of(rel))
        if taken >= limit:
            by_graph[rel.dataset] = 0
            continue
        with ep.trace() as t:
            plan = plan_fetch(
                ep, rel, pattern, stats=Statistics(ctx.workspace, ep, rel), guard=guard_rows()
            )
            check_guard(plan, pattern, rel, max_rows=limit - taken)
            got = execute_fetch(ep, rel, pattern, plan, max_rows=limit - taken)
        records.extend(t.records)
        plans.append(plan.as_dict())
        tables.append(got)
        by_graph[rel.dataset] = got.num_rows
        taken += got.num_rows
    table = pa.concat_tables(tables) if tables else bindings_table(variables, [])
    verdict = completeness(records)
    limit_reached = taken >= limit and not verdict["complete"]
    approximations = (
        [f"stopped at the limit of {limit} rows: which rows, not how many, is unspecified (R3)"]
        if limit_reached
        else []
    )
    receipt = new_receipt(
        kind=operation,
        step=step.step,
        grade="bundle",
        sources=[source_of(rel) for _, rel in resolved],
        shape="bindings",
        pattern=pattern.as_body(),
        requests=request_entries(records),
        totals=totals_of(records),
        completeness=verdict,
        approximations=approximations,
        inputs=inputs,
        detail={"rows_by_graph": by_graph, "limit": limit, "plans": plans},
    )
    info = ctx.workspace.write_table(name, table, receipt)
    step.receipt(info.receipt)
    step.table(info.receipt)
    step.finish()
    return {
        "table": info.name,
        "path": str(info.path),
        "rows": info.rows,
        "columns": variables,
        "grade": "bundle",
        "graphs": by_graph,
        "complete": verdict["complete"],
        "limit_reached": limit_reached,
        "approximations": approximations,
        "requests": len(records),
        "step": step.step,
        "content_hash": info.content_hash,
        "preview": preview(table, variables),
    }


def sql(
    ctx: Context,
    query: str,
    *,
    out: str | None = None,
    max_rows: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """``sql``: one SELECT over the workspace's tables, in the sandboxed DuckDB
    (C8, D6). The result is a new table, which is how tables are joined,
    filtered, grouped and counted here: fetch steps, and SQL to combine them.

    The table is ``bindings`` like any other, so a later step binds from it and
    ``labels`` names its IRIs. A result column gets its kind back by
    ``../kgf-explorer``'s rule (``sandbox.terms_from_sql``); a number SQL
    computed is a typed literal, text with a datatype beside it. Its grade is
    the weakest of the tables it read, one hop (D17), and ``asserted`` where it
    read none, since its values were then typed by the caller."""
    ws = ctx.workspace
    number = ws.next_step()
    name = ws.check_name(out or f"sql_{number}")
    limit = max_rows or sandbox.MAX_ROWS
    budget_s = timeout_s or sandbox.TIMEOUT_S
    started = time.perf_counter()
    selected = sandbox.select(ws, query, max_rows=limit, timeout_s=budget_s)
    variables, rows, plain = sandbox.terms_from_sql(ws, selected)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    step = ctx.step("sql", query=query, max_rows=max_rows, timeout_s=timeout_s, out=name)
    inputs = {table: step.input_table(table) for table in selected.reads}
    grade = weakest_grade(str(i["grade"]) for i in inputs.values()) if inputs else "asserted"
    graphs = list(
        dict.fromkeys(g for table in selected.reads for g in graphs_of(ws.read_receipt(table)))
    )
    approximations = (
        [f"stopped at the row budget of {limit:,}: which rows, not how many, is unspecified"]
        if selected.truncated
        else []
    )
    receipt = new_receipt(
        kind="sql",
        step=step.step,
        grade=grade,
        sources=[],
        shape="bindings",
        requests=[],
        totals={
            "requests": 0,
            "bindings_requests": 0,
            "bytes_decoded": 0,
            "bytes_wire": 0,
            "elapsed_ms": elapsed,
        },
        completeness={
            "complete": not selected.truncated,
            "incomplete_because": ["max_rows"] if selected.truncated else [],
        },
        approximations=approximations,
        inputs=inputs,
        detail={
            "statement": query,
            "reads": selected.reads,
            "graphs": graphs,
            "plain_text": plain,
            "engine": {"duckdb": selected.duckdb},
            "budget": {"max_rows": limit, "timeout_s": budget_s},
        },
    )
    table = bindings_table(variables, rows)
    info = ws.write_table(name, table, receipt)
    step.receipt(info.receipt)
    step.table(info.receipt)
    step.finish()
    handle: dict[str, Any] = {
        "table": info.name,
        "path": str(info.path),
        "rows": info.rows,
        "columns": variables,
        "grade": grade,
        "reads": selected.reads,
        "complete": not selected.truncated,
        "approximations": approximations,
        "step": step.step,
        "content_hash": info.content_hash,
        "preview": preview(table, variables),
    }
    if plain:
        first = next(iter(plain))
        counts = ", ".join(f"{c} ({n:,} of {info.rows:,} rows)" for c, n in plain.items())
        # Stated, never guessed (C6): say which columns are text, and how to
        # make them IRIs, where a fetch bound to them would match nothing.
        handle["note"] = (
            f"Plain text, not IRIs: {counts}. These values had no {first}_kind beside them and "
            f"are not in the tables read with one kind. If they are IRIs, select 'iri' AS "
            f"{first}_kind with {first}."
        )
    return handle
