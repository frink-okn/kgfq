import pytest

from kgfq.client.terms import (
    BadTermSyntax,
    BNode,
    Iri,
    Literal,
    Var,
    compact_iri,
    format_term,
    parse_term,
    term_columns,
    term_from_json,
    term_to_json,
)

PREFIXES = {
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}


def test_iri_round_trip():
    t = parse_term("<http://example.org/a>")
    assert t == Iri("http://example.org/a")
    assert format_term(t) == "<http://example.org/a>"


def test_curie_expands_against_declared_prefix_and_formats_bracketed():
    t = parse_term("rdfs:label", PREFIXES)
    assert t == Iri("http://www.w3.org/2000/01/rdf-schema#label")
    assert format_term(t) == "<http://www.w3.org/2000/01/rdf-schema#label>"


def test_undeclared_prefix_is_an_error_with_the_remedy():
    with pytest.raises(BadTermSyntax, match="bracket it as `<foaf:name>`"):
        parse_term("foaf:name", PREFIXES)


def test_literals():
    assert parse_term('"Diabetes mellitus"@en') == Literal("Diabetes mellitus", lang="en")
    assert parse_term('"42"^^xsd:integer', PREFIXES) == Literal(
        "42", datatype=PREFIXES["xsd"] + "integer"
    )
    assert parse_term('"42"^^<http://www.w3.org/2001/XMLSchema#integer>') == Literal(
        "42", datatype=PREFIXES["xsd"] + "integer"
    )
    assert format_term(Literal('say "hi"\n', lang="en")) == '"say \\"hi\\"\\n"@en'
    assert parse_term(format_term(Literal('say "hi"\n', lang="en"))) == Literal(
        'say "hi"\n', lang="en"
    )


def test_variables_and_bnodes():
    assert parse_term("?x") == Var("x")
    assert parse_term("_:b0") == BNode("b0")
    assert format_term(Var("x")) == "?x"


def test_bare_word_is_refused():
    with pytest.raises(BadTermSyntax):
        parse_term("atrazine")


def test_json_terms():
    obj = {"type": "literal", "value": "atrazine", "lang": "en"}
    assert term_from_json(obj) == Literal("atrazine", lang="en")
    assert term_to_json(Literal("atrazine", lang="en")) == obj
    assert term_from_json({"type": "iri", "value": "http://x"}) == Iri("http://x")
    assert term_columns(None) == (None, None, None, None)


def test_term_columns_state_the_kind():
    xsd = "http://www.w3.org/2001/XMLSchema#"
    assert term_columns(Iri("http://x")) == ("http://x", "iri", None, None)
    assert term_columns(BNode("b0")) == ("_:b0", "bnode", None, None)
    assert term_columns(Literal("1", datatype=xsd + "integer")) == (
        "1",
        "literal",
        None,
        xsd + "integer",
    )
    assert term_columns(Literal("hi", lang="en")) == ("hi", "literal", "en", None)
    # one term, one encoding: a plain literal stores no datatype however it was spelled
    plain = ("36310", "literal", None, None)
    assert term_columns(Literal("36310")) == plain
    assert term_columns(Literal("36310", datatype=xsd + "string")) == plain
    assert term_columns(term_from_json({"type": "literal", "value": "36310"})) == plain


def test_compact_for_display_only():
    assert compact_iri("http://www.w3.org/2000/01/rdf-schema#label", PREFIXES) == "rdfs:label"
    assert compact_iri("http://nowhere/x", PREFIXES) == "http://nowhere/x"
