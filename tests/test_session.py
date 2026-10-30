import pytest

from kgfq.client.endpoint import Endpoint
from kgfq.client.session import AmbiguousDataset, Session, UnknownDataset
from kgfq.workspace.store import Workspace
from tests.conftest import FIXTURES, build_bundle, spawn_server


@pytest.fixture
def session(tmp_path, chem, genes):
    ws = Workspace(tmp_path)
    ws.add_endpoint(chem.origin)
    ws.add_endpoint(genes.origin)
    s = Session(ws)
    yield s
    s.close()


def test_datasets_across_origins_are_graphs_not_servers(session, chem, genes):
    entries = session.datasets()
    assert [(e.id, e.origin) for e in entries] == [
        ("chemkg", chem.origin),
        ("genekg", genes.origin),
    ]
    assert entries[0].triples == 61 and "search" in entries[0].capabilities


def test_resolve_pins_and_reuses_the_pin(session, chem):
    ep, rel = session.resolve("chemkg")
    assert ep.origin == chem.origin and rel.version == "2026-09-01"
    pins = session.workspace.pins()
    assert pins["chemkg"].content_digest == rel.content_digest
    with ep.trace() as t:
        ep2, rel2 = session.resolve("chemkg")
    assert (ep2.origin, rel2.version) == (ep.origin, rel.version)
    assert t.requests == 0, "a pinned dataset resolves without touching the network"


def test_pinned_resolve_makes_no_request(session, chem, monkeypatch):
    """C2: the service descriptor, the release's links and the manifest come
    from the workspace once a graph is pinned; a second session reads nothing."""
    ep, rel = session.resolve("chemkg")
    ep.prefixes(rel)  # the manifest, now cached by digest
    again = Session(session.workspace)

    def refuse(self, *a, **k):
        raise AssertionError(f"request on the pinned path: {a[0] if a else k}")

    monkeypatch.setattr(Endpoint, "get", refuse)
    ep2, rel2 = again.resolve("chemkg")
    assert rel2 == rel and ep2.caps == ep.caps and ep2.prefixes(rel2) == ep.prefixes(rel)
    assert rel2.link("schema") == rel.link("schema")
    again.close()
    session.workspace.clear_cache("descriptors")
    with pytest.raises(AssertionError, match="pinned path"):
        Session(session.workspace).resolve("chemkg")


def test_resolve_by_dataset_iri_and_explicit_origin(session, genes):
    _, rel = session.resolve("https://example.org/kg/genekg")
    assert rel.dataset == "genekg"
    ep, rel = session.resolve(f"genekg@{genes.origin}")
    assert ep.origin == genes.origin


def test_unknown_dataset_names_the_origins_searched(session, chem, genes):
    with pytest.raises(UnknownDataset) as info:
        session.resolve("nope")
    assert chem.origin in str(info.value) and genes.origin in str(info.value)


def test_same_id_with_different_digests_is_an_error_naming_both(tmp_path, chem):
    root = tmp_path / "root2"
    build_bundle(root, "chemkg", "2026-09-02", FIXTURES / "genes.nt", FIXTURES / "chem.build.yaml")
    other = spawn_server(root, tmp_path / "other.log")
    try:
        ws = Workspace(tmp_path / "ws")
        ws.add_endpoint(chem.origin)
        ws.add_endpoint(other.origin)
        s = Session(ws)
        with pytest.raises(AmbiguousDataset) as info:
            s.resolve("chemkg")
        assert chem.origin in str(info.value) and other.origin in str(info.value)
        _ep, rel = s.resolve(f"chemkg@{other.origin}")
        assert rel.version == "2026-09-02"
        s.close()
    finally:
        other.stop()


def test_verified_capabilities_are_probed_once_and_cached(session):
    ep, rel = session.resolve("chemkg")
    with ep.trace() as t:
        verdict = session.verified_capabilities(ep, rel)
    assert verdict["sample"] and verdict["search"] and verdict["labels"]
    assert set(verdict) <= ep.declared_capabilities(rel)
    assert all(isinstance(v, bool) for v in verdict.values())
    assert t.requests >= 3
    with ep.trace() as t2:
        assert session.verified_capabilities(ep, rel) == verdict
    assert t2.requests == 0
