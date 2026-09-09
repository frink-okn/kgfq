"""The linter (plan §5.6): each rule on a minimal query, and the registry corpus."""

import io
import json
from pathlib import Path

import pytest

from kgfq.cli import main
from kgfq.lint import ParseError, analyze, datasets_of, lint, parse_header

P = """PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX ex: <http://example.org/>
"""
CORPUS = Path(__file__).parent.parent.parent / "okn-registry/docs/registry/queries"


def codes(query: str, **kw) -> list[str]:
    return [d.code for d in lint(P + query, **kw)]


def test_clean_query_has_no_diagnostics():
    assert codes("SELECT ?g ?l WHERE { ex:chem1 ex:affects ?g . ?g rdfs:label ?l }") == []


def test_kgfq001_finite_equality_and_in():
    q = "SELECT ?d WHERE { ?d a ?t . FILTER(?t = ex:Disease || ?t = ex:Phenotype) }"
    [d] = lint(P + q)
    assert (
        d.code == "KGFQ001" and "VALUES ?t" in (d.suggestion or "") and len(d.detail["values"]) == 2
    )
    assert codes("SELECT ?d WHERE { ?d a ?t . FILTER(?t IN (ex:A, ex:B, ex:C)) }") == ["KGFQ001"]
    assert codes('SELECT ?d WHERE { ?d rdfs:label ?l . FILTER(?l = "atrazine"@en) }') == ["KGFQ001"]
    # a variable not bound by a required pattern is not a candidate
    assert (
        codes(
            "SELECT ?d WHERE { ?d a ex:Thing . OPTIONAL { ?d ex:p ?v } FILTER(?v = ex:A || ?v = ex:B) }"
        )
        == []
    )


def test_kgfq002_constant_bind():
    assert codes("SELECT ?l WHERE { BIND(ex:chem1 AS ?c) ?c rdfs:label ?l }") == ["KGFQ002"]
    assert codes("SELECT ?l ?x WHERE { ?c rdfs:label ?l . BIND(STR(?l) AS ?x) }") == []


def test_kgfq003_variable_predicate_by_string_filter():
    q = "SELECT ?s ?p ?o WHERE { ?s ?p ?o . FILTER(STRSTARTS(STR(?p), 'http://purl.obolibrary.org/obo/RO_')) }"
    [d] = lint(P + q)
    assert d.code == "KGFQ003" and "predicate inventory" in (d.suggestion or "")
    assert (
        codes(
            "SELECT ?s ?o WHERE { VALUES ?p { ex:a ex:b } ?s ?p ?o . FILTER(STRSTARTS(STR(?p), 'http')) }"
        )
        == []
    )


def test_kgfq004_and_005_connectivity():
    q = """SELECT ?a ?b WHERE {
      ?pert a ex:Perturbagen ; ex:identifier ?id .
      ?comp a ex:Compound ; rdfs:label ?b .
      OPTIONAL { ?comp ex:sameAs ?id }
    }"""
    found = lint(P + q)
    assert [d.code for d in found] == ["KGFQ004", "KGFQ005"]
    assert found[0].severity == "strong" and len(found[0].detail["components"]) == 2
    connected = "SELECT ?a WHERE { ?pert ex:identifier ?id . ?comp ex:sameAs ?id ; rdfs:label ?a }"
    assert codes(connected) == []
    # a theta-join expressed as a FILTER connects the components
    assert codes("SELECT ?x WHERE { ?x ex:v ?a . ?y ex:w ?b . FILTER(?a = ?b) }") == []
    # a constant-subject lookup beside the main pattern is a small product: a warning
    small = lint(
        P + "SELECT ?n ?s WHERE { ?p a ex:Provider ; ex:service ex:TELE . ex:TELE ex:name ?s . }"
    )
    assert [(d.code, d.severity) for d in small] == [("KGFQ004", "warning")] and small[0].detail[
        "small"
    ]
    # union branches count as one connected node
    assert (
        codes("SELECT ?g WHERE { ?c a ex:Compound . { ?c ex:up ?g } UNION { ?c ex:down ?g } }")
        == []
    )


def test_kgfq006_label_text_filter_as_primary_lookup():
    q = "SELECT ?d WHERE { ?d rdfs:label ?l . FILTER(CONTAINS(LCASE(?l), 'alzheimer')) }"
    [d] = lint(P + q)
    assert d.code == "KGFQ006" and "kgfq resolve" in (d.suggestion or "")
    # anchored subject: the filter is a residual check, not the lookup
    assert codes("SELECT ?l WHERE { ex:d1 rdfs:label ?l . FILTER(REGEX(?l, 'alz')) }") == []
    assert (
        codes("SELECT ?l WHERE { VALUES ?d { ex:d1 } ?d rdfs:label ?l . FILTER(REGEX(?l, 'alz')) }")
        == []
    )


def test_kgfq007_limit_after_blocking():
    assert codes("SELECT DISTINCT ?l WHERE { ?s ex:label ?l } LIMIT 100") == ["KGFQ007"]
    assert codes("SELECT ?l WHERE { ?s ex:label ?l } ORDER BY ?l LIMIT 10") == ["KGFQ007"]
    assert codes("SELECT ?l WHERE { ?s ex:label ?l } LIMIT 10") == []


def test_kgfq008_threshold():
    q = "SELECT ?a WHERE { " + " ".join(f"?a ex:p{i} ?v{i} ." for i in range(7)) + " }"
    assert "KGFQ008" in codes(q)
    assert "KGFQ008" not in codes(q, threshold=7)


def test_kgfq009_aggregation_over_multi_valued_joins():
    q = """SELECT ?k (COUNT(DISTINCT ?pub) AS ?n) WHERE {
      ?pub ex:uses ?ds . ?ds ex:keyword ?kw . ?kw rdfs:label ?k } GROUP BY ?k"""
    assert codes(q) == ["KGFQ009"]
    assert codes("SELECT (COUNT(?s) AS ?n) WHERE { ?s a ex:Thing }") == []


def test_kgfq010_and_012_paths():
    assert codes("SELECT ?a ?b WHERE { ?a rdfs:subClassOf* ?b }") == ["KGFQ010"]
    assert codes("SELECT ?a WHERE { ?a rdfs:subClassOf* ex:Root }") == []
    assert codes("SELECT ?a WHERE { VALUES ?b { ex:Root } ?a rdfs:subClassOf+ ?b }") == []
    closed = {"http://www.w3.org/2000/01/rdf-schema#subClassOf"}
    [d] = lint(P + "SELECT ?a WHERE { ?a rdfs:subClassOf* ex:Root }", closed_predicates=closed)
    assert d.code == "KGFQ012" and "without * or +" in (d.suggestion or "")
    assert codes("SELECT ?a WHERE { ?a rdfs:subClassOf ex:Root }", closed_predicates=closed) == []


def test_kgfq011_select_star():
    assert codes("SELECT * WHERE { ?s ex:p ?o }") == ["KGFQ011"]
    assert analyze(P + "SELECT ?s WHERE { ?s ex:p ?o }").select_star is False


def test_service_blocks_are_walked_and_counted():
    q = "SELECT ?l WHERE { SERVICE <http://remote/sparql> { ?d a ex:Disease ; rdfs:label ?l } ?p ex:about ?d }"
    shape = analyze(P + q)
    assert shape.services == 1 and len(shape.patterns) == 3
    assert codes(q) == []


def test_parse_error_is_reported_not_raised_as_crash():
    with pytest.raises(ParseError):
        lint("SELECT ?x WHERE { ?x ns:p ?y }")


def test_registry_header():
    h = parse_header(
        "#+ summary: Genes for a disease\n#+ tags:\n#+   - prokn\n#+   - ubergraph\n\nSELECT * WHERE { ?s ?p ?o }"
    )
    assert h["summary"] == "Genes for a disease" and datasets_of(h) == ["prokn", "ubergraph"]
    assert parse_header("SELECT * WHERE { ?s ?p ?o }") == {}


@pytest.mark.skipif(not CORPUS.exists(), reason="okn-registry checkout not beside this repo")
def test_registry_corpus_lints_and_gates_fire():
    results = {}
    severities = {}
    errors = {}
    for f in sorted(CORPUS.rglob("*.rq")):
        name = str(f.relative_to(CORPUS))
        try:
            found = lint(f.read_text())
        except ParseError as e:
            errors[name] = str(e)
            continue
        results[name] = [d.code for d in found]
        severities[name] = {d.severity for d in found}
    assert len(results) + len(errors) >= 89
    assert set(errors) <= {"prokn/protein_opioid_abuse.rq"}, (
        errors
    )  # an undeclared prefix in the corpus itself
    assert {"KGFQ004", "KGFQ005"} <= set(results["prokn/lincs_kinase_perturbations.rq"])
    assert "KGFQ001" in results["prokn/list_diseases.rq"]
    assert "KGFQ003" in results["ncipidkg/list-all-labeled-interactions.rq"]
    assert "KGFQ006" in results["prokn/alzheimers_associated_genes.rq"]
    assert "KGFQ007" in results["scales/scales-ontology-event-labels.rq"]
    assert "KGFQ009" in results["nasa-gesdisc-kg/frequent-sciencekeywords.rq"]
    assert results["ubergraph/abdominal-cell-types.rq"] == []
    strong = [n for n, sev in severities.items() if "strong" in sev]
    assert strong == ["prokn/lincs_kinase_perturbations.rq"], strong
    # the constant-subject lookup beside a connected pattern is a warning, not a refusal
    assert "KGFQ004" in results["ruralkg/telehealth_treatment_providers.rq"]
    assert "strong" not in severities["ruralkg/telehealth_treatment_providers.rq"]


def test_cli_lint(tmp_path):
    good = tmp_path / "good.rq"
    good.write_text(P + "SELECT ?l WHERE { ex:c1 rdfs:label ?l }")
    bad = tmp_path / "bad.rq"
    bad.write_text(
        "#+ tags:\n#+   - chemkg\n" + P + "SELECT ?a ?b WHERE { ?a ex:p ?x . ?b ex:q ?y }"
    )
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--workspace", str(tmp_path), "--json", "lint", str(good), str(bad)], out=out, err=err
    )
    payload = json.loads(out.getvalue())
    assert code == 1
    assert payload["files"][0]["diagnostics"] == []
    assert payload["files"][1]["datasets"] == ["chemkg"]
    assert payload["files"][1]["diagnostics"][0]["code"] == "KGFQ004"
    out = io.StringIO()
    assert (
        main(
            ["--workspace", str(tmp_path), "--json", "lint", str(bad), "--fail-on", "never"],
            out=out,
            err=err,
        )
        == 0
    )
    broken = tmp_path / "broken.rq"
    broken.write_text("SELECT ?x WHERE { ?x ns:p ?y }")
    assert (
        main(
            ["--workspace", str(tmp_path), "--json", "lint", str(broken)],
            out=io.StringIO(),
            err=err,
        )
        == 2
    )

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    text = Tty()
    main(["--workspace", str(tmp_path), "lint", str(bad)], out=text, err=err)
    assert "KGFQ004 [strong]" in text.getvalue() and "→" in text.getvalue()
