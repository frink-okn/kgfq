"""The session (plan C2, D14): a set of graphs, each at a pinned release on
some origin. The server is a property of the graph; nothing here groups by it.

- Origins come from the workspace's endpoint list (or an explicit list).
- A dataset argument — an id, a dataset IRI, or ``id@origin`` — resolves to one
  graph at one release. The same id at two origins with different digests is
  an error naming both, never a silent pick; the same digest is one graph.
- ``latest``/``current`` is resolved once and pinned in the workspace (D5).
- Capabilities are verified per release and cached (D7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..workspace.store import Pin, Workspace
from .endpoint import Endpoint, Release
from .errors import KgfError, KgfqError, TransportError


class UnknownDataset(KgfqError):
    pass


class AmbiguousDataset(KgfqError):
    pass


@dataclass(frozen=True, slots=True)
class DatasetEntry:
    origin: str
    id: str
    title: str | None
    description: str | None
    triples: int | None
    current: str | None
    capabilities: tuple[str, ...]
    dataset_iri: str | None = None


@dataclass
class Session:
    workspace: Workspace
    skin: str = "kgfq"
    origins_override: list[str] | None = None
    _endpoints: dict[str, Endpoint] = field(default_factory=dict)
    unreachable: dict[str, str] = field(default_factory=dict)

    # -- origins ------------------------------------------------------------

    def origins(self) -> list[str]:
        if self.origins_override is not None:
            return [o.rstrip("/") for o in self.origins_override]
        return self.workspace.endpoints()

    def endpoint(self, origin: str) -> Endpoint:
        origin = origin.rstrip("/")
        if origin not in self._endpoints:
            self._endpoints[origin] = Endpoint(origin, skin=self.skin)
        return self._endpoints[origin]

    def close(self) -> None:
        for e in self._endpoints.values():
            e.close()
        self._endpoints.clear()

    # -- discovery ----------------------------------------------------------

    def datasets(self) -> list[DatasetEntry]:
        """Every graph on every origin, never grouped by server."""
        out: list[DatasetEntry] = []
        self.unreachable = {}
        for origin in self.origins():
            try:
                entries = self.endpoint(origin).datasets()
            except (KgfError, TransportError) as e:
                self.unreachable[origin] = str(e)
                continue
            for d in entries:
                out.append(
                    DatasetEntry(
                        origin=origin,
                        id=d.get("id", ""),
                        title=d.get("title"),
                        description=d.get("description"),
                        triples=d.get("triples"),
                        current=d.get("current"),
                        capabilities=tuple(d.get("capabilities", [])),
                        dataset_iri=d.get("dataset_iri"),
                    )
                )
        return sorted(out, key=lambda e: (e.id, e.origin))

    # -- resolution and pinning ----------------------------------------------

    def resolve(
        self, name: str, *, version: str | None = None, pin: bool = True
    ) -> tuple[Endpoint, Release]:
        origin_hint: str | None = None
        if "@http" in name:
            name, _, origin_hint = name.partition("@")
            origin_hint = origin_hint.rstrip("/")
        pins = self.workspace.pins()
        if version is None and origin_hint is None and name in pins:
            pinned = pins[name]
            if pinned.origin in self.origins():
                ep = self.endpoint(pinned.origin)
                return ep, ep.release(pinned.dataset, pinned.version)
        candidates: list[tuple[Endpoint, Release]] = []
        searched: list[str] = []
        for origin in [origin_hint] if origin_hint else self.origins():
            ep = self.endpoint(origin)
            searched.append(origin)
            try:
                entries = ep.datasets()
            except (KgfError, TransportError) as e:
                self.unreachable[origin] = str(e)
                continue
            by_iri = name.startswith("http://") or name.startswith("https://")
            for d in entries:
                if by_iri:
                    # the service descriptor's entries carry no dataset IRI;
                    # the dataset descriptor does
                    iri = d.get("dataset_iri") or ep.dataset_descriptor(d["id"]).get("dataset_iri")
                    if iri != name:
                        continue
                elif d.get("id") != name:
                    continue
                try:
                    candidates.append((ep, ep.release(d["id"], version)))
                except KeyError:
                    continue
        if not candidates:
            raise UnknownDataset(
                f"no dataset {name!r}"
                + (f" at version {version!r}" if version else "")
                + f" on {', '.join(searched) or 'no endpoints (add one with `kgfq endpoints add`)'}"
            )
        digests = {rel.content_digest for _, rel in candidates}
        if len(digests) > 1:
            listing = "; ".join(
                f"{rel.dataset}@{rel.version} {rel.content_digest} at {ep.origin}"
                for ep, rel in candidates
            )
            raise AmbiguousDataset(
                f"{name!r} resolves to different data at different origins — name one as "
                f"`{name}@<origin>`: {listing}"
            )
        ep, rel = candidates[0]
        if pin and version is None:
            self.workspace.pin(
                rel.dataset,
                Pin(ep.origin, rel.dataset, rel.version, rel.content_digest, rel.dataset_iri),
            )
        return ep, rel

    # -- verified capabilities (D7) ------------------------------------------

    PROBES: ClassVar[dict[str, tuple[str, str, Any]]] = {
        # capability: (method, link name, GET params or QUERY body)
        "sample": ("GET", "sample", {"n": 1, "seed": 0}),
        "search": ("GET", "search", {"q": "a", "limit": 1}),
        "labels": ("QUERY", "labels", {"iris": ["<http://example.org/kgfq-probe>"]}),
        "terms": ("GET", "terms", {"prefix": "http", "limit": 1}),
        "star": (
            "QUERY",
            "star",
            {
                "subjects": ["<http://example.org/kgfq-probe>"],
                "properties": ["<http://example.org/kgfq-probe>"],
            },
        ),
        "export": ("GET", "export", None),
        "closure": ("GET", "closure", {"limit": 1}),
    }

    def verified_capabilities(
        self, endpoint: Endpoint, release: Release, *, refresh: bool = False
    ) -> dict[str, bool]:
        """Probe each declared capability once per release and cache the verdict
        beside the pins. A route that answers anything but 404/501 is available;
        a declared capability with no link is not."""
        cache = self.workspace.cache_dir("capabilities") / (
            release.content_digest.replace(":", "_") + ".json"
        )
        if cache.exists() and not refresh:
            return json.loads(cache.read_text())
        declared = endpoint.declared_capabilities(release)
        artifacts = endpoint.manifest(release).get("artifacts", {})
        verdict: dict[str, bool] = {}
        for cap in sorted(declared):
            if cap in ("filters", "keysets"):
                verdict[cap] = any(k.startswith(cap + "/") for k in artifacts)
                continue
            probe = self.PROBES.get(cap)
            if probe is None:
                verdict[cap] = cap in release.links
                continue
            method, link, payload = probe
            if link not in release.links:
                verdict[cap] = False
                continue
            try:
                if method == "GET":
                    endpoint.get(release.link(link), payload)
                else:
                    endpoint.query(release.link(link), payload)
                verdict[cap] = True
            except KgfError as e:
                verdict[cap] = e.status not in (404, 501) and e.code != "capability_not_available"
            except TransportError:
                verdict[cap] = False
        cache.write_text(json.dumps(verdict, indent=1, sort_keys=True) + "\n")
        return verdict


def dataset_key(name: str) -> str:
    return name.partition("@")[0]
