"""Receipts (plan C6, D5, D17): what makes an approximate answer a finding.

A receipt is a shared ``core`` and a per-operation ``detail`` (2026-11-20,
``notes/session-format.md`` §5), so a client that has never heard of the
operation still reads the grade, the sources, the completeness and the table.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from ..client.endpoint import Release, RequestRecord, Trace

RECEIPT_SCHEMA = "kgf.receipt/1.0"

# The truncations a followed cursor repairs: doc 03 §3.6 promises every cursor
# resumes with no loss and no duplication, budget interruptions included. Only
# `page_limit` has been seen continued (9,671 corpus responses); `response_bytes`
# resuming losslessly is kgf-rs's own test, and `time_budget` is declared there
# but not yet emitted. The reason still counts only when its cursor was followed.
CONTINUABLE = frozenset({"page_limit", "time_budget", "response_bytes"})


def source_of(release: Release) -> dict[str, Any]:
    return {
        "kind": "kgf",
        "grade": "bundle",
        "origin": release.origin,
        "dataset": release.dataset,
        "dataset_iri": release.dataset_iri,
        "version": release.version,
        "content_digest": release.content_digest,
    }


def new_receipt(
    *,
    kind: str,
    step: int | None,
    grade: str,
    sources: Iterable[Mapping[str, Any]],
    completeness: Mapping[str, Any],
    totals: Mapping[str, Any],
    requests: Iterable[Mapping[str, Any]] | None = None,
    shape: str | None = None,
    pattern: Mapping[str, str] | None = None,
    approximations: Iterable[str] = (),
    inputs: Mapping[str, Any] | None = None,
    bindings: Mapping[str, Any] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A receipt in the shared layout (``notes/session-format.md`` §5,
    ``docs/session/receipt.schema.json``): a ``core`` every client reads the
    same way, and the ``detail`` only a client that implements ``kind`` reads.
    Another client's detail is kept as it came and never interpreted.

    ``requests`` is left out where the list lives elsewhere, as a SPARQL run's
    does in the engine's own receipt. The table's name, rows, columns and
    content hash join the core when it is filed (``kgfq.workspace.store``)."""
    core: dict[str, Any] = {
        "kind": kind,
        "step": step,
        "grade": grade,
        "sources": [dict(s) for s in sources],
        "shape": shape,
        "pattern": dict(pattern) if pattern else None,
        "completeness": dict(completeness),
        "totals": dict(totals),
        "approximations": list(approximations),
        "inputs": dict(inputs or {}),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if requests is not None:
        core["requests"] = [dict(r) for r in requests]
    if bindings is not None:
        core["bindings"] = dict(bindings)
    return {"schema": RECEIPT_SCHEMA, "core": core, "detail": dict(detail or {})}


def request_entries(records: Iterable[RequestRecord]) -> list[dict[str, Any]]:
    return [
        {
            "request_id": r.request_id,
            "method": r.method,
            "url": r.url,
            "status": r.status,
            "bytes_wire": r.bytes_wire,
            "bytes_decoded": r.bytes_decoded,
            "elapsed_ms": r.elapsed_ms,
            "complete": r.complete,
            "truncation_reason": r.truncation_reason,
            "continued": r.continued,
            "stopped": r.stopped,
            "bindings": r.bindings,
            "rows": r.rows,
        }
        for r in records
    ]


def completeness(records: Iterable[RequestRecord]) -> dict[str, Any]:
    """A table is incomplete only when its read left rows the server still had:
    it stopped at a response with rows remaining (``max_rows``), or a response
    said ``complete: false`` and was not a :data:`CONTINUABLE` truncation whose
    cursor was followed.
    Only responses that carried the table's rows are read; the plan's ``/count``
    and the statistics' ``/schema`` pages are the plan's evidence, not the table.
    ``truncation_reasons`` is every reason those responses gave, continued or
    not — the trace; ``incomplete_because`` is what the table is missing for."""
    carried = [r for r in records if r.rows is not None]
    because: set[str] = set()
    for r in carried:
        if r.stopped:
            because.add(r.stopped)
        elif r.complete is False and not (r.continued and r.truncation_reason in CONTINUABLE):
            because.add(r.truncation_reason or "unstated")
    return {
        "complete": not because,
        "incomplete_because": sorted(because),
        "truncation_reasons": sorted({r.truncation_reason for r in carried if r.truncation_reason}),
    }


def totals_of(records: Iterable[RequestRecord]) -> dict[str, Any]:
    records = list(records)
    return {
        "requests": len(records),
        "bindings_requests": sum(1 for r in records if r.bindings is not None),
        "bytes_wire": sum(r.bytes_wire for r in records),
        "bytes_decoded": sum(r.bytes_decoded for r in records),
        "elapsed_ms": round(sum(r.elapsed_ms for r in records), 3),
    }


def fetch_receipt(
    *,
    step: int | None,
    kind: str,
    release: Release,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    trace: Trace,
    pattern: Mapping[str, str] | None = None,
    approximations: Iterable[str] = (),
    inputs: Mapping[str, Any] | None = None,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The receipt of a table read from one release. Its ``shape`` is
    ``bindings``; ``pattern`` is the one pattern the table answers, each
    position a term or the ``?variable`` whose column holds it, which is what
    turns a row back into its triple. The intent and the plan (D12) are this
    client's detail."""
    records = trace.records
    return new_receipt(
        kind=kind,
        step=step,
        grade="bundle",
        sources=[source_of(release)],
        shape="bindings",
        pattern=pattern,
        requests=request_entries(records),
        totals=totals_of(records),
        completeness=completeness(records),
        approximations=approximations,
        inputs=inputs,
        bindings=bindings,
        detail={"intent": dict(intent), "plan": dict(plan) if plan else None},
    )


def request_receipt(
    *,
    step: int | None,
    kind: str,
    records: Iterable[RequestRecord],
    release: Release | None = None,
    sources: Iterable[Mapping[str, Any]] | None = None,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The receipt of a step that asked something and wrote no table: a
    summary, a schema page, a count, a sample, of one ``release``; or a listing
    across origins, whose ``sources`` name them. Its log record is the only
    place it is kept."""
    records = list(records)
    return new_receipt(
        kind=kind,
        step=step,
        grade="bundle",
        sources=[source_of(release)] if release is not None else list(sources or []),
        requests=request_entries(records),
        totals=totals_of(records),
        completeness=completeness(records),
        bindings=bindings,
    )


def derived_receipt(
    *, step: int | None, statement: str, inputs: Mapping[str, Mapping[str, Any]], grade: str
) -> dict[str, Any]:
    """A table produced by SQL over workspace tables: one hop of derivation.
    Nothing was requested, so nothing was left behind."""
    return new_receipt(
        kind="derived",
        step=step,
        grade=grade,
        sources=[],
        completeness={"complete": True, "incomplete_because": []},
        totals={"requests": 0, "bindings_requests": 0, "bytes_decoded": 0, "elapsed_ms": 0.0},
        inputs={name: dict(r) for name, r in inputs.items()},
        detail={"statement": statement},
    )
