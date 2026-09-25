"""Receipts (plan C6, D5, D17): what makes an approximate answer a finding."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from ..client.endpoint import Release, RequestRecord, Trace

RECEIPT_SCHEMA = "kgfq.receipt/3"

# The truncations a followed cursor repairs: doc 03 §3.6 promises every cursor
# resumes with no loss and no duplication, budget interruptions included. Only
# `page_limit` has been seen continued (9,671 corpus responses); `response_bytes`
# resuming losslessly is kgf-rs's own test, and `time_budget` is declared there
# but not yet emitted. The reason still counts only when its cursor was followed.
CONTINUABLE = frozenset({"page_limit", "time_budget", "response_bytes"})


def source_of(release: Release) -> dict[str, Any]:
    return {
        "grade": "bundle",
        "origin": release.origin,
        "dataset": release.dataset,
        "dataset_iri": release.dataset_iri,
        "version": release.version,
        "content_digest": release.content_digest,
    }


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


def fetch_receipt(
    *,
    step: int | None,
    kind: str,
    release: Release,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    trace: Trace,
    approximations: Iterable[str] = (),
    inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    records = trace.records
    return {
        "schema": RECEIPT_SCHEMA,
        "kind": kind,
        "step": step,
        "grade": "bundle",
        "source": source_of(release),
        "intent": dict(intent),
        "plan": dict(plan) if plan else None,
        "requests": request_entries(records),
        "totals": {
            "requests": len(records),
            "bytes_wire": sum(r.bytes_wire for r in records),
            "bytes_decoded": sum(r.bytes_decoded for r in records),
            "elapsed_ms": round(sum(r.elapsed_ms for r in records), 3),
        },
        "completeness": completeness(records),
        "approximations": list(approximations),
        "inputs": dict(inputs or {}),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def derived_receipt(
    *, step: int | None, statement: str, inputs: Mapping[str, Mapping[str, Any]], grade: str
) -> dict[str, Any]:
    """A table produced by SQL over workspace tables: one hop of derivation."""
    return {
        "schema": RECEIPT_SCHEMA,
        "kind": "derived",
        "step": step,
        "grade": grade,
        "statement": statement,
        "inputs": {name: dict(r) for name, r in inputs.items()},
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
