"""The endpoint client (plan C1): one KGF origin, spoken to exactly as doc 03
specifies, with the three benchmark traps absorbed here and nowhere else.

- Bindings rows come back keyed positionally (``s``/``p``/``o``) with a
  ``binding`` index relative to the *submitted chunk*; this module re-keys rows
  by the caller's variable names and returns indexes relative to the caller's
  full list.
- A chunk can span several cursor pages; the chunk arithmetic lives here.
- Completeness is read from the ``KGF-*`` headers first and the body second,
  because CSV and Parquet bodies carry it only in headers (D8).

Every request is observed: a :class:`RequestRecord` per response, with the
server's ``KGF-Request-Id``, so receipts (C6) and the access-log census can
join on it. A paged read also marks what the client did with each response —
its cursor followed, or the read stopped there with rows left — since a table's
completeness is that, not the server's per-page flag (:class:`Paged`). Caps and
budgets come from the service descriptor and are never hard-coded (D14: they
are per endpoint).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeGuard
from urllib.parse import urljoin

import httpx

from .. import __version__
from .errors import (
    CapabilityUnavailable,
    KgfError,
    KgfqError,
    TransportError,
    UnboundBindings,
    UncarriedEquality,
)
from .terms import Term, Var, format_term, term_from_json

JSON = "application/json"
PROBLEM = "application/problem+json"


@dataclass(frozen=True, slots=True)
class Pattern:
    """A triple pattern. Each position is a bound term, a named variable, or
    ``None`` for an anonymous variable (omitted from the request)."""

    s: Term | Var | None = None
    p: Term | Var | None = None
    o: Term | Var | None = None

    def bound(self) -> dict[str, Term]:
        return {k: v for k, v in (("s", self.s), ("p", self.p), ("o", self.o)) if _is_term(v)}

    def variables(self) -> dict[str, str]:
        """Position -> name for every unbound position: the caller's, and for an
        anonymous position its own (``s``, ``p``, ``o``) unless a variable of
        the pattern is called that, when it takes a name none has. One name for
        two positions is an equality: ``?o <p> ?`` with its object also called
        ``o`` asks for values ``x`` with ``x <p> x``, and keys a row's subject
        and object alike (C1, 2026-11-20)."""
        taken = set(self.named())
        out: dict[str, str] = {}
        for pos, v in (("s", self.s), ("p", self.p), ("o", self.o)):
            if isinstance(v, Var):
                out[pos] = v.name
            elif v is None:
                name = pos
                if name in taken:
                    name = f"_kgfq_{pos}"
                    while name in taken:
                        name += "_"
                taken.add(name)
                out[pos] = name
        return out

    def named(self) -> list[str]:
        """The variable names the caller wrote, in position order. A bare ``?``
        names nothing, although the wire spells it ``?s``/``?p``/``?o``."""
        return [v.name for v in (self.s, self.p, self.o) if isinstance(v, Var)]

    def repeated(self) -> list[str]:
        """The variables the pattern names in more than one position."""
        named = self.named()
        return sorted({v for v in named if named.count(v) > 1})

    def without_equalities(self) -> Pattern:
        """The pattern with each repeated variable's positions anonymous: what a
        GET answers, and an upper bound on the pattern itself."""
        repeated = self.repeated()
        s, p, o = (
            None if isinstance(v, Var) and v.name in repeated else v
            for v in (self.s, self.p, self.o)
        )
        return Pattern(s, p, o)

    def check_names_are_carried(self) -> None:
        """Refuse a repeated variable before a request that sends no names (C1,
        2026-11-20). A GET carries bound positions only, so ``?x <p> ?x`` went
        out as ``? <p> ?``: on the fixture a count answered 24 for a pattern
        matching nothing, and a fetch wrote 24 rows whose subject was their
        object. With a body the server applies the equality, and refuses it
        itself unless the bindings bind the variable in every row."""
        repeated = self.repeated()
        if repeated:
            raise UncarriedEquality(
                f"`{self.spelled()}` repeats {', '.join('?' + v for v in repeated)}, an "
                "equality the server can only apply when --values binds it in every row"
            )

    def check_bindings(self, vars_: Sequence[str]) -> None:
        """Refuse bindings that name a variable this pattern does not (C1,
        2026-11-06). The server joins a bindings block the way SPARQL joins
        ``VALUES``: a column that shares no variable with the pattern binds
        nothing, and every row answers the whole pattern. Partial overlap is
        refused too, since the two R2 routes answer it differently."""
        named = self.named()
        missing = list(dict.fromkeys(v for v in vars_ if v not in named))
        if not missing:
            return
        positions = (("s", self.s), ("p", self.p), ("o", self.o))
        anonymous = [pos for pos, v in positions if v is None]
        example = None
        if anonymous:
            fill = dict(zip(anonymous, missing, strict=False))
            s, p, o = (Var(fill[pos]) if pos in fill else v for pos, v in positions)
            example = Pattern(s, p, o).spelled()
        one = len(missing) == 1
        has = (
            "whose variables are " + ", ".join(f"?{n}" for n in named)
            if named
            else "which names no variable"
        )
        lines = [
            f"binding {'variable' if one else 'variables'} "
            f"{', '.join(f'?{v}' for v in missing)} {'does' if one else 'do'} not appear "
            f"in the pattern `{self.spelled()}`, {has}"
            + (" (a bare `?` is anonymous)." if anonymous else "."),
        ]
        if len(missing) == len(set(vars_)):
            lines += [
                "A binding variable the pattern does not name binds nothing: every row would "
                "answer the whole pattern, and a count or fetch would repeat it once per row.",
                f"Name the position it binds, e.g. `{example}`."
                if example
                else "Rename the column or the pattern variable so the two match.",
            ]
        else:
            lines += [
                "A column the pattern does not name binds nothing: rows that differ only "
                "there would repeat, and a bind-join and a filtered fetch would disagree.",
                "Leave it out and join it back on a bound variable's column, "
                + (
                    f"or name the position it binds, e.g. `{example}`."
                    if example
                    else "or rename it to match a pattern variable."
                ),
            ]
        raise UnboundBindings(" ".join(lines))

    def spelled(self) -> str:
        """The pattern as the caller writes it: ``?`` for an anonymous position."""
        return " ".join(
            "?" if v is None else str(v) if isinstance(v, Var) else format_term(v)
            for v in (self.s, self.p, self.o)
        )

    def as_params(self) -> dict[str, str]:
        return {pos: format_term(t) for pos, t in self.bound().items()}

    def as_body(self) -> dict[str, str]:
        """The pattern as a body spells it: every position present, each
        variable by the name :meth:`variables` gives it."""
        names = self.variables()
        return {
            pos: format_term(v) if _is_term(v) else f"?{names[pos]}"
            for pos, v in (("s", self.s), ("p", self.p), ("o", self.o))
        }


def _is_term(v: object) -> TypeGuard[Term]:
    return v is not None and not isinstance(v, Var)


@dataclass(frozen=True, slots=True)
class Release:
    """A dataset at a pinned, immutable release on one origin (D5, D14)."""

    origin: str
    dataset: str
    version: str
    content_digest: str
    dataset_iri: str | None
    links: Mapping[str, str]

    def link(self, name: str) -> str:
        try:
            return self.links[name]
        except KeyError:
            raise KeyError(
                f"release {self.dataset}@{self.version} publishes no `{name}` link"
            ) from None


@dataclass(frozen=True, slots=True)
class Count:
    value: int
    exact: bool
    min: int | None = None

    @classmethod
    def from_json(cls, obj: Any) -> Count:
        if isinstance(obj, int):
            return cls(obj, True)
        return cls(int(obj["value"]), bool(obj.get("exact", True)), obj.get("min"))


@dataclass(frozen=True, slots=True)
class DistinctValue:
    """One distinct value of a pattern position: the term, the triples holding
    it, and under bindings how many input rows reached it."""

    term: Term
    count: int
    bindings: int | None = None


@dataclass(frozen=True, slots=True)
class Distinct:
    """Every distinct value of one position (doc 03 §3.4.13), read to the end,
    and the triples matched."""

    group_by: str
    values: list[DistinctValue]
    triples: int


@dataclass(slots=True)
class RequestRecord:
    """One response, as the receipt and the access-log census see it.

    ``complete`` and ``truncation_reason`` are the server's, verbatim.
    ``continued`` and ``stopped`` are the client's: it followed this response's
    cursor, or its read ended here while the server still had rows, and why."""

    method: str
    url: str
    status: int
    request_id: str | None
    elapsed_ms: float
    bytes_wire: int
    bytes_decoded: int
    complete: bool | None
    truncation_reason: str | None
    bindings: int | None = None
    rows: int | None = None
    continued: bool = False
    stopped: str | None = None


@dataclass(slots=True)
class Trace:
    records: list[RequestRecord] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return len(self.records)

    @property
    def bytes_wire(self) -> int:
        return sum(r.bytes_wire for r in self.records)

    @property
    def request_ids(self) -> list[str]:
        return [r.request_id for r in self.records if r.request_id]


class Response:
    """A KGF JSON response with the uniform completeness vocabulary attached."""

    def __init__(self, raw: httpx.Response, record: RequestRecord) -> None:
        self.raw = raw
        self.record = record
        ctype = raw.headers.get("content-type", "")
        self.body: Any = raw.json() if raw.content and (JSON in ctype or PROBLEM in ctype) else None
        headers = raw.headers
        self.complete = _header_bool(headers.get("kgf-complete"))
        if self.complete is None and isinstance(self.body, dict):
            self.complete = self.body.get("complete")
        self.truncation_reason = headers.get("kgf-truncation-reason") or (
            self.body.get("truncation_reason") if isinstance(self.body, dict) else None
        )
        self.next = headers.get("kgf-next-cursor") or (
            self.body.get("next") if isinstance(self.body, dict) else None
        )
        record.complete = self.complete
        record.truncation_reason = self.truncation_reason

    @property
    def request_id(self) -> str | None:
        return self.record.request_id


def _header_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() == "true"


class Paged[T](Iterator[T]):
    """The rows of one paged read, and, once its caller is done, whether it left
    any the server still had. A generator cannot say that after its caller breaks
    off; this can, without another request.

    ``pages`` yields each response with its rows and whether it is the read's
    last: its own final page, and for bindings the final chunk's. Following a
    cursor marks that response ``continued``. A caller that stops early says why
    with :meth:`stop`; when rows remained — the rest of the page in hand, a cursor
    not followed, a chunk not sent — the response it stopped at is marked
    ``stopped``. A receipt reads a table's completeness from those marks (C6)."""

    def __init__(
        self, pages: Iterator[tuple[Response, list[T], bool]], *, max_rows: int | None = None
    ) -> None:
        self._pages = pages
        self._max_rows = max_rows
        self._page: Response | None = None
        self._rows: list[T] = []
        self._at = 0
        self._last = False
        self.taken = 0

    def __next__(self) -> T:
        if self._max_rows is not None and self.taken >= self._max_rows:
            self.stop("max_rows")
            raise StopIteration
        while self._at >= len(self._rows):
            if self._last:
                raise StopIteration
            page = next(self._pages, None)
            if page is None:  # no chunk to send: an empty bindings list
                self._last = True
                raise StopIteration
            self._page, self._rows, self._last = page
            self._at = 0
        row = self._rows[self._at]
        self._at += 1
        self.taken += 1
        return row

    @property
    def exhausted(self) -> bool:
        """The read's last page is in hand and every row of it taken."""
        return self._last and self._at >= len(self._rows)

    def stop(self, reason: str) -> None:
        """The caller is done. Mark the response it stopped at, if rows remained."""
        if self._page is not None and not self.exhausted and self._page.record.stopped is None:
            self._page.record.stopped = reason


class Endpoint:
    """One KGF origin: descriptors, caps, and the operations doc 03 names."""

    def __init__(
        self,
        origin: str,
        *,
        skin: str = "kgfq",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.origin = origin.rstrip("/")
        self.skin = skin
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": f"kgf-client/{__version__} ({skin})",
                "Accept": JSON,
            },
            follow_redirects=False,
        )
        self._service: dict[str, Any] | None = None
        self._datasets: dict[str, dict[str, Any]] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._query_supported: bool | None = None
        self._traces: list[Trace] = []

    # -- observation --------------------------------------------------------

    @contextmanager
    def trace(self) -> Iterator[Trace]:
        t = Trace()
        self._traces.append(t)
        try:
            yield t
        finally:
            self._traces.remove(t)

    def _observe(self, record: RequestRecord) -> None:
        for t in self._traces:
            t.records.append(record)

    # -- transport ----------------------------------------------------------

    def _url(self, path: str) -> str:
        """Descriptor links are root-relative paths that already carry the
        deployment's public-base prefix (doc 03 §3.2), so they resolve against
        the host, never by appending to an origin that has the prefix too."""
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(self.origin + "/", path)

    def _base(self, path: str = "") -> str:
        """A path this client spells itself — the service descriptor at the
        base, or a dataset descriptor by id — lives under the origin, prefix
        and all. Only links from the server are root-relative."""
        return self.origin + "/" + path.lstrip("/")

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        accept: str = JSON,
    ) -> Response:
        url = self._url(path)
        content = None
        headers = {"Accept": accept}
        if body is not None:
            content = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
            headers["Content-Type"] = JSON
        attempts = 0
        while True:
            attempts += 1
            started = time.perf_counter()
            try:
                raw = self._client.request(
                    method, url, params=params, content=content, headers=headers
                )
            except httpx.HTTPError as e:
                raise TransportError(f"{method} {url}: {e}") from e
            elapsed = (time.perf_counter() - started) * 1000
            record = RequestRecord(
                method=method,
                url=str(raw.request.url),
                status=raw.status_code,
                request_id=raw.headers.get("kgf-request-id"),
                elapsed_ms=round(elapsed, 3),
                bytes_wire=raw.num_bytes_downloaded,
                bytes_decoded=len(raw.content),
                complete=None,
                truncation_reason=None,
            )
            self._observe(record)
            # The origin advertising QUERY does not mean the path to it allows
            # it; a refusal, once observed, is the better evidence (D9).
            if raw.headers.get("accept-query") and self._query_supported is None:
                self._query_supported = True
            if raw.status_code in (307, 308):
                location = raw.headers.get("location")
                if not location:
                    raise TransportError(f"{method} {url}: redirect without Location")
                url = self._url(location)
                continue
            if raw.status_code >= 400:
                problem = _problem(raw)
                error = KgfError(
                    problem,
                    status=raw.status_code,
                    url=str(raw.request.url),
                    request_id=record.request_id,
                )
                if error.retryable and attempts < 3:
                    retry_after = raw.headers.get("retry-after")
                    time.sleep(
                        min(float(retry_after), 5.0)
                        if retry_after and retry_after.isdigit()
                        else 0.2 * attempts
                    )
                    continue
                raise error
            ctype = raw.headers.get("content-type", "")
            if accept == JSON and JSON not in ctype:
                raise TransportError(f"{method} {url}: expected JSON, got {ctype!r}")
            return Response(raw, record)

    def get(
        self, path: str, params: Mapping[str, Any] | None = None, *, accept: str = JSON
    ) -> Response:
        return self._send("GET", path, params=params, accept=accept)

    def query(self, path: str, body: Any) -> Response:
        """A body-carrying read: QUERY, with POST as the per-origin fallback (D9)."""
        if self._query_supported is False:
            return self._send("POST", path, body=body)
        try:
            response = self._send("QUERY", path, body=body)
        except KgfError as e:
            if e.code != "method_not_allowed" and e.status != 405:
                raise
            self._query_supported = False
            return self._send("POST", path, body=body)
        except TransportError:
            if self._query_supported:
                raise
            self._query_supported = False
            return self._send("POST", path, body=body)
        self._query_supported = True
        return response

    @property
    def query_supported(self) -> bool | None:
        return self._query_supported

    # -- descriptors --------------------------------------------------------

    def service_descriptor(self) -> dict[str, Any]:
        if self._service is None:
            self._service = self.get(self._base()).body
        return self._service

    @property
    def caps(self) -> dict[str, int]:
        return dict(self.service_descriptor().get("caps", {}))

    @property
    def budgets(self) -> dict[str, int]:
        return dict(self.service_descriptor().get("budgets", {}))

    def cap(self, name: str, default: int) -> int:
        return int(self.caps.get(name, default))

    def datasets(self) -> list[dict[str, Any]]:
        return list(self.service_descriptor().get("datasets", []))

    def dataset_descriptor(self, dataset: str) -> dict[str, Any]:
        if dataset not in self._datasets:
            entry = next((d for d in self.datasets() if d.get("id") == dataset), None)
            path = entry["url"] if entry and entry.get("url") else self._base(dataset)
            self._datasets[dataset] = self.get(path).body
        return self._datasets[dataset]

    def release(self, dataset: str, version: str | None = None) -> Release:
        """Resolve a dataset (and ``current`` when no version is given) to an
        immutable release, following the descriptor's links rather than
        spelling routes (doc 03 §3.2)."""
        descriptor = self.dataset_descriptor(dataset)
        wanted = version or descriptor.get("current")
        for rel in descriptor.get("releases", []):
            if rel.get("version") == wanted:
                return Release(
                    origin=self.origin,
                    dataset=descriptor.get("id", dataset),
                    version=rel["version"],
                    content_digest=rel.get("content_digest", ""),
                    dataset_iri=descriptor.get("dataset_iri"),
                    links=dict(rel.get("links", {})),
                )
        raise KeyError(f"{self.origin}: dataset {dataset!r} has no release {wanted!r}")

    def link_url(self, release: Release, name: str) -> str:
        """A release link as the absolute URL another program can be handed."""
        return self._url(release.link(name))

    def manifest(self, release: Release) -> dict[str, Any]:
        key = release.content_digest or f"{release.dataset}@{release.version}"
        if key not in self._manifests:
            self._manifests[key] = self.get(release.link("manifest")).body
        return self._manifests[key]

    def prefixes(self, release: Release) -> dict[str, str]:
        return dict(self.manifest(release).get("prefixes", {}))

    def declared_capabilities(self, release: Release) -> set[str]:
        caps = self.manifest(release).get("capabilities", {})
        return set(caps.keys()) if isinstance(caps, dict) else set(caps)

    # -- operations ---------------------------------------------------------

    def summary(self, release: Release, fmt: str = "json") -> Any:
        if fmt == "json":
            return self.get(release.link("summary"), {"format": "json"}).body
        raw = self._send(
            "GET", release.link("summary"), params={"format": fmt}, accept="text/markdown, */*"
        )
        return raw.raw.text

    def schema_page(self, release: Release, params: Mapping[str, Any] | None = None) -> Response:
        return self.get(release.link("schema"), dict(params or {}))

    def schema_items(
        self,
        release: Release,
        params: Mapping[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Every item of a paged `/schema` collection or projection, transparently."""
        params = dict(params or {})
        params.setdefault("limit", self.cap("max_schema_items", 1000))
        yielded = 0
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            page = self.schema_page(release, params)
            for item in (page.body or {}).get("items", []):
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            if page.complete or not page.next:
                return
            page.record.continued = True
            cursor = page.next

    def fragment_page(
        self,
        release: Release,
        pattern: Pattern,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Response:
        pattern.check_names_are_carried()
        params: dict[str, Any] = dict(pattern.as_params())
        params["limit"] = limit or self.cap("max_limit", 10000)
        if cursor:
            params["cursor"] = cursor
        if extra:
            params.update(extra)
        response = self.get(release.link("fragment"), params)
        response.record.rows = len((response.body or {}).get("rows", []))
        return response

    def fragment(
        self,
        release: Release,
        pattern: Pattern,
        *,
        limit: int | None = None,
        max_rows: int | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Paged[dict[str, Term]]:
        """Rows keyed by the caller's variable names, paged transparently."""
        names = pattern.variables()

        def pages() -> Iterator[tuple[Response, list[dict[str, Term]], bool]]:
            cursor = None
            while True:
                page = self.fragment_page(release, pattern, limit=limit, cursor=cursor, extra=extra)
                last = bool(page.complete or not page.next)
                yield page, [_rekey(row, names) for row in page.body.get("rows", [])], last
                if last:
                    return
                page.record.continued = True
                cursor = page.next

        return Paged(pages(), max_rows=max_rows)

    def count(self, release: Release, pattern: Pattern) -> Count:
        pattern.check_names_are_carried()
        body = self.get(release.link("count"), pattern.as_params()).body
        return Count.from_json(body["count"])

    def sample(
        self, release: Release, pattern: Pattern, *, n: int = 25, seed: int = 0
    ) -> list[dict[str, Term]]:
        pattern.check_names_are_carried()
        params: dict[str, Any] = dict(pattern.as_params())
        params.update({"n": min(n, self.cap("max_sample", 1000)), "seed": seed})
        body = self.get(release.link("sample"), params).body
        names = pattern.variables()
        return [_rekey(r, names) for r in body.get("rows", [])]

    # -- bindings (the workhorse) -------------------------------------------

    def _chunks(
        self,
        rows: Sequence[Sequence[Term]],
        vars_: Sequence[str],
        template: dict[str, Any],
        cap_name: str,
    ) -> Iterator[tuple[int, list[list[str]]]]:
        """Chunk by both ``max_bindings`` and ``max_request_bytes`` (D9)."""
        max_rows = self.cap(cap_name, 200)
        max_bytes = int(self.budgets.get("max_request_bytes", 1 << 20))
        formatted = [[format_term(t) for t in r] for r in rows]
        start = 0
        while start < len(formatted):
            size = min(max_rows, len(formatted) - start)
            while True:
                chunk = formatted[start : start + size]
                probe = dict(template)
                probe["bindings"] = {"vars": [f"?{v}" for v in vars_], "rows": chunk}
                encoded = len(json.dumps(probe, separators=(",", ":"), sort_keys=True).encode())
                if encoded <= max_bytes or size == 1:
                    break
                size = max(1, size // 2)
            yield start, chunk
            start += size

    def bindings(
        self,
        release: Release,
        pattern: Pattern,
        vars_: Sequence[str],
        rows: Sequence[Sequence[Term]],
        *,
        limit: int | None = None,
    ) -> Paged[tuple[int, dict[str, Term]]]:
        """``QUERY /fragment`` with bindings: yields ``(index, row)`` where
        ``index`` is into the caller's ``rows`` and ``row`` is keyed by the
        caller's variable names. Chunking and cross-page cursors are handled
        here; the server's chunk-relative ``binding`` never escapes. The read's
        last page is the final chunk's: a caller that stops after an earlier
        chunk's last page has left the unsent chunks behind. Bindings that name
        a variable the pattern does not are refused before any request."""
        pattern.check_bindings(vars_)
        names = pattern.variables()
        template: dict[str, Any] = {
            "pattern": pattern.as_body(),
            "limit": limit or self.cap("max_limit", 10000),
        }

        def pages() -> Iterator[tuple[Response, list[tuple[int, dict[str, Term]]], bool]]:
            for offset, chunk in self._chunks(rows, vars_, template, "max_bindings"):
                final_chunk = offset + len(chunk) >= len(rows)
                body = dict(template)
                body["bindings"] = {"vars": [f"?{v}" for v in vars_], "rows": chunk}
                while True:
                    page = self.query(release.link("fragment"), body)
                    page.record.bindings = len(chunk)
                    page.record.rows = len(page.body.get("rows", []))
                    end = bool(page.complete or not page.next)
                    got = [
                        (offset + int(row["binding"]), _rekey(row, names))
                        for row in page.body.get("rows", [])
                    ]
                    yield page, got, end and final_chunk
                    if end:
                        break
                    page.record.continued = True
                    body["cursor"] = page.next

        return Paged(pages())

    def count_bindings(
        self,
        release: Release,
        pattern: Pattern,
        vars_: Sequence[str],
        rows: Sequence[Sequence[Term]],
    ) -> list[Count]:
        """``QUERY /count`` with bindings: one count per caller row, in order.
        Bindings that name a variable the pattern does not are refused first."""
        pattern.check_bindings(vars_)
        template: dict[str, Any] = {"pattern": pattern.as_body()}
        out: list[Count | None] = [None] * len(rows)
        for offset, chunk in self._chunks(rows, vars_, template, "max_bindings"):
            body = dict(template)
            body["bindings"] = {"vars": [f"?{v}" for v in vars_], "rows": chunk}
            cursor = None
            while True:
                if cursor:
                    body["cursor"] = cursor
                page = self.query(release.link("count"), body)
                page.record.bindings = len(chunk)
                for entry in page.body.get("counts", []):
                    out[offset + int(entry["binding"])] = Count.from_json(entry["count"])
                if page.complete or not page.next:
                    break
                page.record.continued = True
                cursor = page.next
        return [c if c is not None else Count(0, True) for c in out]

    # -- distinct values (doc 03 §3.4.13) ------------------------------------

    def distinct(
        self,
        release: Release,
        pattern: Pattern,
        group_by: str,
        *,
        vars_: Sequence[str] | None = None,
        rows: Sequence[Sequence[Term]] | None = None,
        limit: int | None = None,
    ) -> Distinct:
        """The distinct values of one position of ``pattern``, with counts.

        ``GET /distinct`` without bindings, every page followed; ``QUERY
        /distinct`` per bindings chunk otherwise. Every page is followed,
        including the empty ones a server's subject scan ends at its candidate
        budget, so the answer is whole. The server merges the rows of one chunk;
        a term reached from several chunks is merged here, its counts and its
        ``bindings`` summed, since chunks hold disjoint rows. Values come back in
        the server's index order within a chunk, and in first-seen order across
        chunks — order carries no meaning (§3.4.13). A release whose server does
        not route ``/distinct`` is refused before any request."""
        if group_by not in ("s", "p", "o"):
            raise KgfqError(f"group_by is a position, s, p or o, not {group_by!r}")
        if group_by in pattern.bound():
            raise KgfqError(
                f"`{pattern.spelled()}` binds {group_by}, so it has one value there; "
                "group by a variable position, or count it"
            )
        if rows is None:
            pattern.check_names_are_carried()
        if "distinct" not in release.links:
            raise CapabilityUnavailable(
                f"{release.dataset}@{release.version} on {self.origin} publishes no `distinct` "
                "link: its server predates doc 03 §3.4.13. Fetch the pattern and group it "
                "with `kgfq sql` instead."
            )
        size = limit or self.cap("max_limit", 10000)
        values: dict[str, DistinctValue] = {}
        cardinality = 0

        def follow(page: Response, previous: str | None) -> str | None:
            """The cursor to send next, or ``None`` after the last page. A page
            that says it is incomplete must carry a cursor, and the cursor must
            move; anything else would end in a silent partial answer or a loop."""
            if page.complete or not page.next:
                if page.complete is False:
                    raise KgfqError(
                        f"{page.record.url}: an incomplete page without a cursor "
                        f"({page.body.get('truncation_reason')})"
                    )
                return None
            if page.next == previous:
                raise KgfqError(f"{page.record.url}: the cursor did not advance")
            page.record.continued = True
            return page.next

        def add(row: Mapping[str, Any], with_bindings: bool) -> None:
            term = term_from_json(row[group_by])
            key = format_term(term)
            count = int(row["count"])
            reached = int(row["bindings"]) if with_bindings else None
            seen = values.get(key)
            if seen is None:
                values[key] = DistinctValue(term, count, reached)
            else:
                values[key] = DistinctValue(
                    term,
                    seen.count + count,
                    None if reached is None else (seen.bindings or 0) + reached,
                )

        if rows is None:
            params: dict[str, Any] = dict(pattern.as_params())
            params.update({"group_by": group_by, "limit": size})
            while True:
                page = self.get(release.link("distinct"), params)
                page.record.rows = len(page.body.get("rows", []))
                for row in page.body.get("rows", []):
                    add(row, False)
                cursor = follow(page, params.get("cursor"))
                if cursor is None:
                    cardinality = Count.from_json(page.body["cardinality"]).value
                    break
                params["cursor"] = cursor
            return Distinct(group_by, list(values.values()), cardinality)

        pattern.check_bindings(vars_ or [])
        template: dict[str, Any] = {
            "pattern": pattern.as_body(),
            "group_by": group_by,
            "limit": size,
        }
        for _offset, chunk in self._chunks(rows, vars_ or [], template, "max_bindings"):
            body = dict(template)
            body["bindings"] = {"vars": [f"?{v}" for v in vars_ or []], "rows": chunk}
            while True:
                page = self.query(release.link("distinct"), body)
                page.record.bindings = len(chunk)
                page.record.rows = len(page.body.get("rows", []))
                for row in page.body.get("rows", []):
                    add(row, True)
                cursor = follow(page, body.get("cursor"))
                if cursor is None:
                    cardinality += Count.from_json(page.body["cardinality"]).value
                    break
                body["cursor"] = cursor
        return Distinct(group_by, list(values.values()), cardinality)

    def labels(self, release: Release, iris: Sequence[str]) -> dict[str, str | None]:
        """``QUERY /labels`` in batches of ``max_label_iris``; missing → ``None``."""
        out: dict[str, str | None] = {iri: None for iri in iris}
        batch = self.cap("max_label_iris", 10000)
        unique = list(out)
        for start in range(0, len(unique), batch):
            chunk = unique[start : start + batch]
            page = self.query(release.link("labels"), {"iris": [f"<{i}>" for i in chunk]})
            for entry in page.body.get("labels", []):
                iri = (
                    entry["iri"]["value"]
                    if isinstance(entry.get("iri"), dict)
                    else entry.get("iri")
                )
                out[iri] = entry.get("label")
        return out

    def search(
        self,
        release: Release,
        q: str,
        *,
        limit: int | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Response:
        params: dict[str, Any] = {"q": q, "limit": limit or 25}
        if extra:
            params.update(extra)
        return self.get(release.link("search"), params)

    def close(self) -> None:
        self._client.close()


def _rekey(row: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Term]:
    out: dict[str, Term] = {}
    for pos, name in names.items():
        if pos in row and row[pos] is not None:
            out[name] = term_from_json(row[pos])
    return out


def _problem(raw: httpx.Response) -> dict[str, Any]:
    try:
        body = raw.json()
        if isinstance(body, dict):
            return body
    except ValueError:
        pass
    return {
        "status": raw.status_code,
        "title": raw.reason_phrase,
        "detail": raw.text[:2000],
        "code": "unknown",
    }


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)
