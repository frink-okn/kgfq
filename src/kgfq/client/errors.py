"""Errors, carrying the server's problem document verbatim.

The benchmark called KGF's problem documents excellent for an LLM client, and
the plan's §6 rule is that nothing in this stack paraphrases them. ``KgfError``
therefore keeps the whole document and formats itself from ``code`` and
``detail``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class KgfqError(Exception):
    """Base for everything this library raises on purpose."""


class KgfError(KgfqError):
    """An RFC 9457 problem document from a KGF server (doc 03 §3.6.1)."""

    def __init__(
        self,
        problem: Mapping[str, Any],
        *,
        status: int,
        url: str,
        request_id: str | None = None,
    ) -> None:
        self.problem = dict(problem)
        self.status = status
        self.url = url
        self.request_id = request_id
        code = self.problem.get("code") or "unknown"
        detail = self.problem.get("detail") or self.problem.get("title") or ""
        super().__init__(f"{code} ({status}): {detail}")

    @property
    def code(self) -> str:
        return str(self.problem.get("code") or "unknown")

    @property
    def detail(self) -> str:
        return str(self.problem.get("detail") or "")

    @property
    def retryable(self) -> bool:
        """§3.6.1: the two codes whose remedy is to retry the identical request."""
        return self.code in ("rate_limited", "internal_error")


class TransportError(KgfqError):
    """The server could not be reached, or answered something that is not KGF."""


class CapabilityUnavailable(KgfqError):
    """A verified-capability check (D7) refused an operation before any request."""


class UnboundBindings(KgfqError):
    """Bindings name a variable the pattern does not (C1, 2026-11-06), refused
    before any request: such a column binds nothing, so each row would answer
    the whole pattern."""


class UncarriedEquality(KgfqError):
    """A pattern repeats a variable and the request cannot carry the equality
    (C1, 2026-11-20), refused before any request: a GET sends bound positions
    and no names, so ``?x <p> ?x`` would be answered as ``? <p> ?``."""


class BadColumns(KgfqError):
    """A pattern whose fetched table could not be given its columns (C6,
    2026-11-20), refused before any request: a fetched table's columns are the
    pattern's variables, so each needs a name of its own."""
