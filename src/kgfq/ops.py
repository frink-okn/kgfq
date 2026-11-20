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

from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from .client.endpoint import Endpoint, Release
from .client.errors import CapabilityUnavailable, KgfqError
from .client.session import Session
from .client.terms import (
    Iri,
    Literal,
    Term,
    format_term,
    term_columns,
    term_from_json,
    variable_columns,
)
from .workspace.bind import bind_rows, unmatched_report
from .workspace.log import Step
from .workspace.receipts import completeness, new_receipt, request_entries, source_of, totals_of
from .workspace.store import Workspace, WorkspaceError

SEARCH_LIMIT = 20
SEARCH_VARIABLES = ["subject", "label", "predicate", "matched", "match_kind", "score"]
LABEL_VARIABLES = ["iri", "label", "graph"]


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
    source = ws.read_receipt(table)["core"]
    asked = list(graphs or [])
    if not asked:
        asked = [
            f"{s['dataset']}@{s['origin']}"
            for s in source.get("sources", [])
            if s.get("kind") == "kgf"
        ]
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
