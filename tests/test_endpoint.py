"""C1 golden tests: the endpoint client against two live fixture servers."""

from collections.abc import Iterator

import pytest

from kgfq.client.endpoint import Count, Endpoint, Pattern, Release
from kgfq.client.errors import KgfError
from kgfq.client.terms import Iri, Literal, Var, format_term
from tests.conftest import spawn_server
from tests.proxy import Proxy

EX = "http://example.org/"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
LABEL = Iri(RDFS + "label")
AFFECTS = Iri(EX + "affects")


@pytest.fixture(scope="module")
def ep(chem) -> Iterator[Endpoint]:
    e = Endpoint(chem.origin)
    yield e
    e.close()


@pytest.fixture(scope="module")
def rel(ep) -> Release:
    return ep.release("chemkg")


def test_descriptors_and_release_pinning(ep, rel):
    assert ep.caps["max_bindings"] >= 1 and ep.budgets["max_request_bytes"] > 0
    assert rel.dataset == "chemkg" and rel.version == "2026-09-01"
    assert rel.content_digest.startswith("sha256:")
    assert rel.dataset_iri == "https://example.org/kg/chemkg"
    manifest = ep.manifest(rel)
    assert manifest["content_digest"] == rel.content_digest
    assert ep.prefixes(rel)["ex"] == EX
    assert "search" in ep.declared_capabilities(rel)


def test_latest_redirect_is_followed(ep):
    body = ep.get("/chemkg/latest/manifest").body
    assert body["version"] == "2026-09-01"


def test_problem_documents_surface_verbatim(ep, rel):
    with pytest.raises(KgfError) as info:
        ep.get(rel.link("fragment"), {"p": "nosuch:x"})
    err = info.value
    assert err.code == "bad_term_syntax" and err.status == 400
    assert "undeclared prefix" in err.detail
    assert err.request_id
    assert err.problem["instance"].endswith("/fragment")


SHAPES = {
    "? p ?": Pattern(p=LABEL),
    "s ? ?": Pattern(s=Iri(EX + "chem/1")),
    "? ? o": Pattern(o=Iri(EX + "gene/G1")),
    "? ? ?": Pattern(),
    "s p ?": Pattern(s=Iri(EX + "chem/1"), p=AFFECTS),
    "? p o": Pattern(p=AFFECTS, o=Iri(EX + "gene/G2")),
    "? p literal": Pattern(p=LABEL, o=Literal("atrazine", lang="en")),
}


@pytest.mark.parametrize("shape", list(SHAPES))
@pytest.mark.parametrize("page", [1, 2, 3, 7, 10000])
def test_every_shape_paged_at_adversarial_sizes_matches_count(ep, rel, shape, page):
    pattern = SHAPES[shape]
    expected = ep.count(rel, pattern)
    assert expected.exact
    with ep.trace() as trace:
        rows = list(ep.fragment(rel, pattern, limit=page))
    assert len(rows) == expected.value
    assert len({tuple(sorted((k, str(v)) for k, v in r.items())) for r in rows}) == len(rows), (
        "duplicates across pages"
    )
    assert trace.requests == -(-expected.value // page) or expected.value == 0
    assert all(r.request_id for r in trace.records)


def test_rows_are_keyed_by_the_callers_names(ep, rel):
    rows = list(ep.fragment(rel, Pattern(s=Var("chem"), p=LABEL, o=Var("name"))))
    assert set(rows[0]) == {"chem", "name"}
    assert any(r["name"] == Literal("atrazine", lang="en") for r in rows)


def test_bindings_chunk_spanning_pages_matches_the_unrestricted_fragment(ep, rel):
    subjects = [Iri(f"{EX}chem/{i}") for i in (1, 2, 3, 5, 8)]
    expected = {
        (r["s"], r["o"]) for r in ep.fragment(rel, Pattern(p=AFFECTS)) if r["s"] in subjects
    }
    # limit=1 forces every chunk to span pages; max_bindings=2 forces several chunks.
    ep._service["caps"]["max_bindings"] = 2
    try:
        with ep.trace() as trace:
            got = list(
                ep.bindings(
                    rel,
                    Pattern(s=Var("c"), p=AFFECTS, o=Var("g")),
                    ["c"],
                    [[s] for s in subjects],
                    limit=1,
                )
            )
    finally:
        ep._service = None
    assert {(row["c"], row["g"]) for _, row in got} == expected
    for index, row in got:
        assert row["c"] == subjects[index], "binding index must be relative to the caller's list"
    assert trace.requests > len(expected) - 1
    assert ep.query_supported is True


def test_bindings_chunk_by_request_bytes(ep, rel):
    ep.service_descriptor()
    ep._service["budgets"]["max_request_bytes"] = 260
    try:
        subjects = [[Iri(f"{EX}chem/{i}")] for i in range(1, 13)]
        with ep.trace() as trace:
            got = list(ep.bindings(rel, Pattern(s=Var("c"), p=LABEL, o=Var("l")), ["c"], subjects))
    finally:
        ep._service = None
    assert len(got) == 12 and [i for i, _ in got] == list(range(12))
    assert trace.requests > 1
    assert max(r.bindings for r in trace.records) < 12


def test_post_takes_over_when_query_is_refused(chem, rel, monkeypatch):
    e = Endpoint(chem.origin)
    original = e._send
    seen = []

    def refusing(method, path, **kw):
        seen.append(method)
        if method == "QUERY":
            raise KgfError({"code": "method_not_allowed", "status": 405}, status=405, url=path)
        return original(method, path, **kw)

    monkeypatch.setattr(e, "_send", refusing)
    counts = e.count_bindings(
        rel, Pattern(s=Var("c"), p=LABEL, o=Var("l")), ["c"], [[Iri(EX + "chem/1")]]
    )
    assert counts == [Count(1, True)]
    assert seen[:2] == ["QUERY", "POST"] or "POST" in seen
    assert e.query_supported is False
    e.count_bindings(rel, Pattern(s=Var("c"), p=LABEL, o=Var("l")), ["c"], [[Iri(EX + "chem/2")]])
    assert seen[-1] == "POST" and seen.count("QUERY") == 1
    e.close()


def test_count_bindings_align_with_caller_rows(ep, rel):
    rows = [[Iri(EX + "chem/1")], [Iri(EX + "nowhere")], [Iri(EX + "chem/2")]]
    counts = ep.count_bindings(rel, Pattern(s=Var("c"), p=AFFECTS, o=Var("g")), ["c"], rows)
    assert [c.value for c in counts] == [2, 0, 2]


def test_sample_is_deterministic_per_seed(ep, rel):
    a = ep.sample(rel, Pattern(p=LABEL), n=3, seed=42)
    b = ep.sample(rel, Pattern(p=LABEL), n=3, seed=42)
    c = ep.sample(rel, Pattern(p=LABEL), n=3, seed=43)
    assert a == b and len(a) == 3
    assert a != c or len(a) == 0


def test_labels_and_search(ep, rel):
    labels = ep.labels(rel, [EX + "chem/1", EX + "nowhere"])
    assert labels[EX + "chem/1"] == "atrazine"
    assert labels[EX + "nowhere"] is None
    hits = ep.search(rel, "atrazine", limit=5).body["results"]
    assert hits and hits[0]["subject"]["value"] == EX + "chem/1"


def test_schema_items_page_transparently(ep, rel):
    items = list(ep.schema_items(rel, {"children": "properties", "limit": 2}))
    predicates = {i["term"]["value"] for i in items}
    assert {RDFS + "label", EX + "affects", EX + "mass"} <= predicates
    assert all("distinct_subjects" in i["counts"] for i in items)


def test_summary_card(ep, rel):
    card = ep.summary(rel)
    assert card["counts"]["triples"] == 61
    assert card["dataset"]["description"] == "Chemicals and the genes they affect."
    assert "Chem KG" in ep.summary(rel, "md")


def test_links_behind_a_public_base_prefix_resolve_against_the_host(chem, tmp_path):
    """A gateway mounts the server under /kgf and the server emits links that
    carry the prefix (doc 03 §3.2); appending them to the origin would double it."""
    prefixed = spawn_server(
        chem.root, tmp_path / "prefixed.log", public_base="http://placeholder/kgf"
    )
    proxy = Proxy(prefixed.origin, prefix="/kgf")
    try:
        e = Endpoint(proxy.origin)
        rel = e.release("chemkg")
        assert rel.link("fragment").startswith("/kgf/chemkg/")
        assert e.count(rel, Pattern(p=LABEL)).value == 12
        assert e.manifest(rel)["id"] == "chemkg"
        assert all(r.url.startswith(proxy.origin + "/") for r in _all_records(e, rel))
        e.close()
    finally:
        proxy.stop()
        prefixed.stop()


def _all_records(e, rel):
    with e.trace() as t:
        e.get(rel.link("count"), {"p": format_term(LABEL)})
    return t.records


def test_query_refused_by_a_real_proxy_falls_back_to_post(chem):
    proxy = Proxy(chem.origin, prefix="", refuse_query=True)
    try:
        e = Endpoint(proxy.origin)
        rel = e.release("chemkg")
        rows = [[Iri(EX + "chem/1")], [Iri(EX + "chem/2")]]
        counts = e.count_bindings(rel, Pattern(s=Var("c"), p=AFFECTS, o=Var("g")), ["c"], rows)
        assert [c.value for c in counts] == [2, 2]
        assert e.query_supported is False
        assert proxy.methods_seen.count("QUERY") == 1 and "POST" in proxy.methods_seen
        list(e.bindings(rel, Pattern(s=Var("c"), p=AFFECTS, o=Var("g")), ["c"], rows))
        assert proxy.methods_seen.count("QUERY") == 1, "a refusal is remembered per origin"
        e.close()
    finally:
        proxy.stop()


def test_an_anonymous_position_takes_a_name_no_variable_has():
    """A position's own name where it is free, since that is what the wire and
    a sample's header have always shown; another where a variable took it."""
    assert Pattern(p=AFFECTS, o=Var("g")).variables() == {"s": "s", "o": "g"}
    clash = Pattern(s=Var("o"), p=AFFECTS)
    assert clash.variables() == {"s": "o", "o": "_kgfq_o"}
    assert clash.as_body() == {"s": "?o", "p": format_term(AFFECTS), "o": "?_kgfq_o"}
    assert Pattern(s=Var("_kgfq_o"), p=Var("o")).variables()["o"] == "_kgfq_o_"
    assert Pattern().as_body() == {"s": "?s", "p": "?p", "o": "?o"}
