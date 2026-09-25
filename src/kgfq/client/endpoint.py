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
join on it. Caps and budgets come from the service descriptor and are never
hard-coded (D14: they are per endpoint).
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
from .errors import KgfError, TransportError
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
        """Position -> caller's name for every unbound position."""
        out: dict[str, str] = {}
        for pos, v in (("s", self.s), ("p", self.p), ("o", self.o)):
            if isinstance(v, Var):
                out[pos] = v.name
            elif v is None:
                out[pos] = pos
        return out

    def as_params(self) -> dict[str, str]:
        return {pos: format_term(t) for pos, t in self.bound().items()}

    def as_body(self) -> dict[str, str]:
        body: dict[str, str] = {}
        for pos, v in (("s", self.s), ("p", self.p), ("o", self.o)):
            if _is_term(v):
                body[pos] = format_term(v)
            elif isinstance(v, Var):
                body[pos] = f"?{v.name}"
            else:
                body[pos] = f"?{pos}"
        return body


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


@dataclass(slots=True)
class RequestRecord:
    """One response, as the receipt and the access-log census see it."""

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
    ) -> Iterator[dict[str, Term]]:
        """Rows keyed by the caller's variable names, paged transparently."""
        names = pattern.variables()
        cursor = None
        yielded = 0
        while True:
            page = self.fragment_page(release, pattern, limit=limit, cursor=cursor, extra=extra)
            for row in page.body.get("rows", []):
                yield _rekey(row, names)
                yielded += 1
                if max_rows is not None and yielded >= max_rows:
                    return
            if page.complete or not page.next:
                return
            cursor = page.next

    def count(self, release: Release, pattern: Pattern) -> Count:
        body = self.get(release.link("count"), pattern.as_params()).body
        return Count.from_json(body["count"])

    def sample(
        self, release: Release, pattern: Pattern, *, n: int = 25, seed: int = 0
    ) -> list[dict[str, Term]]:
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
    ) -> Iterator[tuple[int, dict[str, Term]]]:
        """``QUERY /fragment`` with bindings: yields ``(index, row)`` where
        ``index`` is into the caller's ``rows`` and ``row`` is keyed by the
        caller's variable names. Chunking and cross-page cursors are handled
        here; the server's chunk-relative ``binding`` never escapes."""
        names = pattern.variables()
        template: dict[str, Any] = {
            "pattern": pattern.as_body(),
            "limit": limit or self.cap("max_limit", 10000),
        }
        for offset, chunk in self._chunks(rows, vars_, template, "max_bindings"):
            body = dict(template)
            body["bindings"] = {"vars": [f"?{v}" for v in vars_], "rows": chunk}
            cursor = None
            while True:
                if cursor:
                    body["cursor"] = cursor
                else:
                    body.pop("cursor", None)
                page = self.query(release.link("fragment"), body)
                page.record.bindings = len(chunk)
                page.record.rows = len(page.body.get("rows", []))
                for row in page.body.get("rows", []):
                    yield offset + int(row["binding"]), _rekey(row, names)
                if page.complete or not page.next:
                    break
                cursor = page.next

    def count_bindings(
        self,
        release: Release,
        pattern: Pattern,
        vars_: Sequence[str],
        rows: Sequence[Sequence[Term]],
    ) -> list[Count]:
        """``QUERY /count`` with bindings: one count per caller row, in order."""
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
                cursor = page.next
        return [c if c is not None else Count(0, True) for c in out]

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
