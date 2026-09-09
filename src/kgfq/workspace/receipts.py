"""Receipts (plan C6, D5, D17): what makes an approximate answer a finding."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from ..client.endpoint import Release, RequestRecord, Trace

RECEIPT_SCHEMA = "kgfq.receipt/1"


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
            "bindings": r.bindings,
            "rows": r.rows,
        }
        for r in records
    ]


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
    truncations = sorted({r.truncation_reason for r in records if r.truncation_reason})
    complete = all(r.complete is not False for r in records)
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
        "completeness": {"complete": complete, "truncation_reasons": truncations},
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
