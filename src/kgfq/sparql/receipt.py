"""The receipt of a SPARQL run (C6, D5, D17), kind ``sparql``: the engine's own
receipt wrapped, not copied.

Decided 2026-09-10: ``kgf-sparql``'s request list is about 500 bytes per request
pretty-printed, half of it URL — 2.5 MB at a 5,000-request abort and 10 MB at
the corpus's 20,000 budget — and 43 of the 94 corpus receipts exceed 500
requests. That list is the evidence the planner fixes of 2026-09-10 were made
from and the client side of the access-log join, so it is kept whole, in the
engine's receipt filed beside the table under its own name and pointed at by
path and digest. The kgfq receipt — which is also the Parquet footer and the
log record's summary — carries the totals, the outcome, the budget, and the
requests grouped by shape, which is the view those fixes were read from."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..workspace.log import weakest_grade
from ..workspace.receipts import RECEIPT_SCHEMA
from .engine import Budget, EngineRun


def requests_by_shape(requests: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The request list folded by what was asked: method, route, which of
    ``s``/``p``/``o`` were bound, the predicate when one was, and whether the
    request pushed bindings. The corpus's worst receipt (5,000 requests) folds
    to 14 shapes, and the top two are the diagnosis."""
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in requests:
        url = str(r.get("url", ""))
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        bound = "".join(k for k in "spo" if k in query)
        predicate = unquote(query["p"][0]) if "p" in query else None
        pushes = bool(r.get("bindings"))
        key = (
            r.get("method"),
            f"{parts.scheme}://{parts.netloc}{parts.path}",
            bound,
            predicate,
            pushes,
        )
        g = groups.setdefault(
            key,
            {
                "method": r.get("method"),
                "url": key[1],
                "bound": bound,
                "predicate": predicate,
                "bindings": pushes,
                "requests": 0,
                "bytes": 0,
                "elapsed_ms": 0.0,
                "statuses": Counter(),
                "failed": 0,
            },
        )
        g["requests"] += 1
        g["bytes"] += int(r.get("bytes") or 0)
        g["elapsed_ms"] = round(g["elapsed_ms"] + float(r.get("elapsed_ms") or 0.0), 1)
        if r.get("status") is not None:
            g["statuses"][str(r["status"])] += 1
        if r.get("error"):
            g["failed"] += 1
    out = sorted(groups.values(), key=lambda g: (-g["requests"], g["url"]))
    for g in out:
        g["statuses"] = dict(sorted(g["statuses"].items()))
    return out


def sparql_receipt(
    *,
    step: int | None,
    sources: list[dict[str, Any]],
    query: str,
    original_query: str | None,
    normalizations: list[dict[str, Any]],
    lint: dict[str, Any],
    run: EngineRun,
    budget: Budget,
    engine_receipt: dict[str, Any] | None,
    inputs: Mapping[str, Any] | None = None,
    approximations: Iterable[str] = (),
) -> dict[str, Any]:
    """Wrap one engine run. ``engine_receipt`` is the pointer — path relative to
    the workspace, sha256, schema — to the receipt the engine wrote."""
    doc = run.receipt or {}
    outcome = dict(doc.get("outcome") or {})
    if not doc:
        outcome = {"contract": "error", "rows": 0, "error": run.error}
    elif outcome.get("contract") == "error" and not outcome.get("error"):
        outcome["error"] = run.error
    requests = list(doc.get("requests") or [])
    totals = dict(doc.get("totals") or {})
    wire = [int(r["bytes_wire"]) for r in requests if r.get("bytes_wire") is not None]
    truncations = sorted(
        {str(r["truncation_reason"]) for r in requests if r.get("truncation_reason")}
    )
    engine = dict(doc.get("engine") or {})
    engine["command"] = list(run.command)
    engine["exit_code"] = run.exit_code
    return {
        "schema": RECEIPT_SCHEMA,
        "kind": "sparql",
        "step": step,
        "grade": weakest_grade(str(s.get("grade", "asserted")) for s in sources),
        "sources": [dict(s) for s in sources],
        "query": query,
        "original_query": original_query,
        "normalizations": list(normalizations),
        "lint": dict(lint),
        "engine": engine,
        "budget": {
            **budget.as_dict(),
            "max_connections": (doc.get("budget") or {}).get("max_connections"),
        },
        "outcome": outcome,
        "totals": {
            "requests": int(totals.get("requests", len(requests))),
            "bindings_requests": int(totals.get("bindings_requests", 0)),
            "schema_requests": int((doc.get("statistics") or {}).get("schema_requests", 0)),
            "bytes_decoded": int(totals.get("bytes", 0)),
            "bytes_wire": sum(wire) if wire else None,
            "elapsed_ms": float(totals.get("elapsed_ms", run.elapsed_ms)),
        },
        "statistics": dict(doc.get("statistics") or {}),
        "requests_by_shape": requests_by_shape(requests),
        "engine_receipt": engine_receipt,
        "completeness": {
            "complete": outcome.get("contract") == "complete",
            "truncation_reasons": truncations,
        },
        "approximations": list(approximations),
        "inputs": dict(inputs or {}),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
