"""Statistics (plan C3): the planner's inputs, cached per release forever.

Everything the strategy function reads before it makes a data request lives
here — the predicate and class inventories from ``/schema``, the
class-properties and class-relations projections, and ``/count`` in both its
single-pattern and batch-bindings forms — keyed by the release's content digest
and cached under ``.kgfq/cache/stats/``, because releases are immutable.

Numbers come from the ``queryable`` view: doc 03 §3.4.10 makes its counts equal
``/count``'s, where the ``design`` view is closure-uncontaminated and may not.
So an exact inventory number answers a count with zero requests, which is what
makes rule R1 "nearly free" (implications §3.1).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..workspace.store import Workspace
from .endpoint import Count, Endpoint, Pattern, Release
from .terms import Iri, Term, format_term

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


@dataclass(frozen=True, slots=True)
class PredicateStats:
    iri: str
    triples: int
    distinct_subjects: int | None
    distinct_objects: int | None


@dataclass(frozen=True, slots=True)
class ClassStats:
    iri: str
    entities: int
    triples: int


@dataclass(frozen=True, slots=True)
class ClassProperty:
    class_iri: str
    predicate: str
    triples: int


@dataclass(frozen=True, slots=True)
class ClassRelation:
    subject_class: str
    predicate: str
    object_class: str
    triples: int


@dataclass(frozen=True, slots=True)
class Estimate:
    """A cardinality the inventory can supply without a request. ``exact`` means
    it equals what ``/count`` would say; otherwise it is a VoID-style average."""

    value: int | None
    exact: bool
    source: str


def canonical_pattern(pattern: Pattern) -> str:
    """The cache key of a single count: its bound positions in §3.3 syntax,
    sorted — all that a single ``/count`` sends."""
    return json.dumps(pattern.as_params(), sort_keys=True, separators=(",", ":"))


def canonical_bindings(pattern: Pattern, vars_: Sequence[str]) -> str:
    """The cache-key prefix of a per-binding count: the pattern as the bindings
    body sends it, every position with its variable named, then the bound
    variables in order (C3, 2026-11-06). Bound positions alone cannot tell
    ``?c p ?g`` from ``?g p ?c`` under a ``?c`` file, and the server answers
    the two differently."""
    body = json.dumps(pattern.as_body(), sort_keys=True, separators=(",", ":"))
    return body + "|" + ",".join(vars_) + "|"


def _iri(obj: Any) -> str:
    if isinstance(obj, dict):
        return str(obj.get("value"))
    return str(obj)


class Statistics:
    def __init__(
        self, workspace: Workspace, endpoint: Endpoint, release: Release, *, view: str = "queryable"
    ) -> None:
        self.workspace = workspace
        self.endpoint = endpoint
        self.release = release
        self.view = view
        slug = release.content_digest.replace(":", "_") or f"{release.dataset}_{release.version}"
        self.dir = workspace.cache_dir("stats") / slug
        self._memo: dict[str, Any] = {}
        self._counts: dict[str, Count] | None = None

    # -- the cache ----------------------------------------------------------

    def _cached(self, name: str, load: Callable[[], Any]) -> Any:
        if name in self._memo:
            return self._memo[name]
        path = self.dir / f"{name}.json"
        if path.exists():
            value = json.loads(path.read_text())
        else:
            value = load()
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(value, sort_keys=True))
            os.replace(tmp, path)
        self._memo[name] = value
        return value

    def cached_names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json")) if self.dir.exists() else []

    # -- inventories --------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return self._cached("summary", lambda: self.endpoint.summary(self.release))

    def root(self) -> dict[str, Any]:
        """The dataset node: totals and the links beneath it."""
        return self._cached(
            "root", lambda: self.endpoint.schema_page(self.release, {"view": self.view}).body
        )

    def totals(self) -> dict[str, int]:
        return dict((self.root().get("node") or {}).get("counts", {}))

    def items(self, children: str) -> list[dict[str, Any]]:
        """Every item of a root collection (``properties`` or ``classes``), in the
        server's own item shape so callers can render it unchanged."""
        return self._cached(
            children,
            lambda: list(
                self.endpoint.schema_items(self.release, {"children": children, "view": self.view})
            ),
        )

    def predicates(self) -> dict[str, PredicateStats]:
        out: dict[str, PredicateStats] = {}
        for item in self.items("properties"):
            iri = _iri(item.get("term"))
            counts = item.get("counts", {})
            out[iri] = PredicateStats(
                iri,
                int(counts.get("triples", 0)),
                counts.get("distinct_subjects"),
                counts.get("distinct_objects"),
            )
        return out

    def classes(self) -> dict[str, ClassStats]:
        out: dict[str, ClassStats] = {}
        for item in self.items("classes"):
            iri = _iri(item.get("term"))
            counts = item.get("counts", {})
            out[iri] = ClassStats(
                iri, int(counts.get("entities", 0)), int(counts.get("triples", 0))
            )
        return out

    def _projection(
        self, name: str, class_iri: str | None, predicate: str | None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"projection": name, "view": self.view}
        key = name
        if class_iri:
            params["class"] = f"<{class_iri}>"
        if predicate:
            params["predicate"] = f"<{predicate}>"
        if class_iri or predicate:
            key += "." + hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]
        return self._cached(key, lambda: list(self.endpoint.schema_items(self.release, params)))

    def class_properties(
        self, class_iri: str | None = None, predicate: str | None = None
    ) -> list[ClassProperty]:
        return [
            ClassProperty(_iri(i.get("class")), _iri(i.get("predicate")), int(i.get("triples", 0)))
            for i in self._projection("class-properties", class_iri, predicate)
        ]

    def class_relations(
        self, class_iri: str | None = None, predicate: str | None = None
    ) -> list[ClassRelation]:
        out = []
        for i in self._projection("class-relations", class_iri, predicate):
            subject = i.get("subject_class") or i.get("subject-class") or i.get("class")
            obj = i.get("object_class") or i.get("object-class")
            out.append(
                ClassRelation(
                    _iri(subject), _iri(i.get("predicate")), _iri(obj), int(i.get("triples", 0))
                )
            )
        return out

    # -- estimates without a request ----------------------------------------

    def estimate(self, pattern: Pattern) -> Estimate:
        """What the inventory says about a pattern's cardinality, with no
        request. Exact for ``? ? ?``, ``? p ?`` and ``? rdf:type C``; a VoID-style
        average when one more position is bound; unknown otherwise."""
        bound = pattern.bound()
        s, p, o = bound.get("s"), bound.get("p"), bound.get("o")
        if not bound:
            return Estimate(self.totals().get("triples"), True, "inventory:dataset")
        if isinstance(p, Iri):
            stats = self.predicates().get(p.value)
            if stats is None:
                return Estimate(0, True, "inventory:predicate-absent")
            if s is None and o is None:
                return Estimate(stats.triples, True, "inventory:predicate")
            if p.value == RDF_TYPE and isinstance(o, Iri) and s is None:
                cls = self.classes().get(o.value)
                return Estimate(cls.entities if cls else 0, True, "inventory:class")
            if s is None and stats.distinct_objects:
                return Estimate(
                    max(1, round(stats.triples / stats.distinct_objects)),
                    False,
                    "inventory:predicate/avg-per-object",
                )
            if o is None and stats.distinct_subjects:
                return Estimate(
                    max(1, round(stats.triples / stats.distinct_subjects)),
                    False,
                    "inventory:predicate/avg-per-subject",
                )
            return Estimate(None, False, "unknown")
        if p is None and (s is None) != (o is None):
            totals = self.totals()
            role = "distinct_subjects" if s is not None else "distinct_objects"
            if totals.get(role):
                which = "subject" if s is not None else "object"
                return Estimate(
                    max(1, round(totals["triples"] / totals[role])),
                    False,
                    f"inventory:dataset/avg-per-{which}",
                )
        return Estimate(None, False, "unknown")

    # -- counts, cached per release ------------------------------------------

    def _count_cache(self) -> dict[str, Count]:
        if self._counts is None:
            self._counts = {}
            path = self.dir / "counts.jsonl"
            if path.exists():
                for line in path.read_text().splitlines():
                    if line.strip():
                        rec = json.loads(line)
                        self._counts[rec["key"]] = Count(rec["value"], rec["exact"], rec.get("min"))
        return self._counts

    def _remember(self, key: str, count: Count) -> None:
        self._count_cache()[key] = count
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "counts.jsonl", "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"key": key, "value": count.value, "exact": count.exact, "min": count.min}
                )
                + "\n"
            )

    def count_source(self, pattern: Pattern) -> tuple[Count, str]:
        """A count and where it came from: ``inventory``, ``cache``, or ``server``.
        A repeated variable is refused before the cache or the inventory is read:
        both key on bound positions, as the GET does."""
        pattern.check_names_are_carried()
        key = canonical_pattern(pattern)
        cached = self._count_cache().get(key)
        if cached is not None:
            return cached, "cache"
        estimate = self.estimate(pattern)
        if estimate.exact and estimate.value is not None:
            return Count(estimate.value, True), estimate.source
        count = self.endpoint.count(self.release, pattern)
        self._remember(key, count)
        return count, "server"

    def count(self, pattern: Pattern) -> Count:
        return self.count_source(pattern)[0]

    def count_bindings(
        self, pattern: Pattern, vars_: Sequence[str], rows: Sequence[Sequence[Term]]
    ) -> list[Count]:
        """One count per row, in order; only rows the cache has not seen go to
        the server, in one batch. Bindings the pattern does not name are refused
        before the cache is read."""
        pattern.check_bindings(vars_)
        base = canonical_bindings(pattern, vars_)
        keys = [base + "\t".join(format_term(t) for t in row) for row in rows]
        cache = self._count_cache()
        missing = [i for i, k in enumerate(keys) if k not in cache]
        if missing:
            fetched = self.endpoint.count_bindings(
                self.release, pattern, vars_, [rows[i] for i in missing]
            )
            for i, count in zip(missing, fetched, strict=True):
                self._remember(keys[i], count)
        return [cache[k] for k in keys]


def stats_dir(workspace: Workspace, release: Release) -> Path:
    return workspace.cache_dir("stats") / release.content_digest.replace(":", "_")
