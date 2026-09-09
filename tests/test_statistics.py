"""C3: the statistics layer, cached per release, against live fixture servers."""

from collections.abc import Iterator

import pytest

from kgfq.client.endpoint import Endpoint, Pattern
from kgfq.client.statistics import ClassRelation, Statistics, canonical_pattern
from kgfq.client.terms import Iri, Var
from kgfq.plan import plan_fetch
from kgfq.workspace.store import Workspace

EX = "http://example.org/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
AFFECTS = Iri(EX + "affects")


@pytest.fixture(scope="module")
def ep(chem) -> Iterator[Endpoint]:
    e = Endpoint(chem.origin)
    yield e
    e.close()


@pytest.fixture
def stats(ep, tmp_path) -> Statistics:
    return Statistics(Workspace(tmp_path), ep, ep.release("chemkg"))


def test_inventories_are_fetched_once_and_cached_under_the_digest(stats, ep):
    with ep.trace() as t:
        predicates = stats.predicates()
        classes = stats.classes()
        totals = stats.totals()
    assert t.requests == 3
    affects = predicates[EX + "affects"]
    assert (affects.triples, affects.distinct_subjects, affects.distinct_objects) == (24, 12, 5)
    assert classes[EX + "Chemical"].entities == 12
    assert totals["triples"] == 61 and totals["properties"] == 5
    assert stats.release.content_digest.replace(":", "_") in str(stats.dir)
    assert set(stats.cached_names()) >= {"properties", "classes", "root"}
    fresh = Statistics(stats.workspace, ep, stats.release)
    with ep.trace() as t2:
        assert fresh.predicates() == predicates and fresh.classes() == classes
    assert t2.requests == 0


def test_inventory_counts_equal_count_for_every_predicate(stats, ep):
    """Doc 03 §3.4.10: the queryable view's numbers equal /count's."""
    for iri, p in stats.predicates().items():
        assert ep.count(stats.release, Pattern(p=Iri(iri))).value == p.triples


def test_projections(stats, genes, tmp_path):
    by_class = stats.class_properties(class_iri=EX + "Chemical")
    assert {(c.predicate, c.triples) for c in by_class} >= {(EX + "affects", 24), (EX + "mass", 12)}
    assert all(c.class_iri == EX + "Chemical" for c in by_class)
    by_pred = stats.class_properties(predicate=EX + "affects")
    assert [(c.class_iri, c.triples) for c in by_pred] == [(EX + "Chemical", 24)]
    gep = Endpoint(genes.origin)
    try:
        gstats = Statistics(Workspace(tmp_path / "g"), gep, gep.release("genekg"))
        relations = gstats.class_relations()
        assert ClassRelation(EX + "Gene", EX + "inPathway", EX + "Pathway", 7) in relations
        assert gstats.class_relations(class_iri=EX + "Gene") == [
            r for r in relations if r.subject_class == EX + "Gene"
        ]
    finally:
        gep.close()


def test_estimates_without_a_request(stats, ep):
    stats.predicates()
    stats.classes()
    stats.totals()
    with ep.trace() as t:
        e = stats.estimate(Pattern())
        assert (e.value, e.exact, e.source) == (61, True, "inventory:dataset")
        e = stats.estimate(Pattern(p=AFFECTS))
        assert (e.value, e.exact, e.source) == (24, True, "inventory:predicate")
        e = stats.estimate(Pattern(p=Iri(RDF_TYPE), o=Iri(EX + "Chemical")))
        assert (e.value, e.exact, e.source) == (12, True, "inventory:class")
        e = stats.estimate(Pattern(s=Iri(EX + "chem/1"), p=AFFECTS))
        assert (e.value, e.exact, e.source) == (2, False, "inventory:predicate/avg-per-subject")
        e = stats.estimate(Pattern(p=AFFECTS, o=Iri(EX + "gene/G1")))
        assert (e.value, e.exact) == (5, False)
        e = stats.estimate(Pattern(s=Iri(EX + "chem/1")))
        assert e.exact is False and e.value == 5 and e.source == "inventory:dataset/avg-per-subject"
        e = stats.estimate(Pattern(p=Iri(EX + "nosuch")))
        assert (e.value, e.exact) == (0, True)
    assert t.requests == 0


def test_counts_come_from_inventory_then_cache_then_server(stats, ep):
    stats.predicates()
    with ep.trace() as t:
        count, source = stats.count_source(Pattern(p=AFFECTS))
    assert (count.value, source, t.requests) == (24, "inventory:predicate", 0)
    with ep.trace() as t:
        count, source = stats.count_source(Pattern(s=Iri(EX + "chem/1"), p=AFFECTS))
    assert (count.value, source, t.requests) == (2, "server", 1)
    with ep.trace() as t:
        count, source = stats.count_source(Pattern(s=Iri(EX + "chem/1"), p=AFFECTS))
    assert (count.value, source, t.requests) == (2, "cache", 0)
    fresh = Statistics(stats.workspace, ep, stats.release)
    with ep.trace() as t:
        assert fresh.count(Pattern(s=Iri(EX + "chem/1"), p=AFFECTS)).value == 2
    assert t.requests == 0, "the count cache is on disk, not in memory"
    assert (stats.dir / "counts.jsonl").exists()
    assert (
        canonical_pattern(Pattern(p=AFFECTS, s=Iri("http://x")))
        == '{"p":"<http://example.org/affects>","s":"<http://x>"}'
    )


def test_batch_counts_send_only_the_rows_the_cache_has_not_seen(stats, ep):
    pattern = Pattern(s=Var("c"), p=AFFECTS, o=Var("g"))
    rows = [[Iri(f"{EX}chem/{i}")] for i in (1, 2, 3)]
    with ep.trace() as t:
        first = stats.count_bindings(pattern, ["c"], rows)
    assert [c.value for c in first] == [2, 2, 2] and t.requests == 1
    with ep.trace() as t:
        assert stats.count_bindings(pattern, ["c"], rows) == first
    assert t.requests == 0
    more = rows + [[Iri(f"{EX}chem/9")]]
    with ep.trace() as t:
        counts = stats.count_bindings(pattern, ["c"], more)
    assert [c.value for c in counts] == [2, 2, 2, 2]
    assert t.requests == 1 and t.records[0].bindings == 1


def test_plan_uses_the_inventory_for_r1(stats, ep):
    stats.predicates()
    with ep.trace() as t:
        plan = plan_fetch(ep, stats.release, Pattern(p=AFFECTS), stats=stats)
    assert t.requests == 0
    assert plan.evidence["count"] == {"value": 24, "exact": True, "source": "inventory:predicate"}
