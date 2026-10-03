# Writing SPARQL that a fragments client can answer

For coding agents and people who bring their own SPARQL to `kgfq sparql`. Every
rule here is also a linter diagnostic (`kgfq lint`), with the code in brackets,
and every rule comes from a measurement in the KGF benchmark, not from taste.
The client-suite report (`kgf-client-suite-query-report.md`, beside this guide)
is the source; this is the same guidance in the order you meet the problems.

The one rule behind all of them: **make selectivity explicit in the algebra.**
A fragments engine plans from triple patterns and published counts. It cannot
see selectivity hidden in a `FILTER`, a `BIND`, a string function, or the
order you wrote the patterns in.

## 1. Finite sets go in `VALUES`, not in `FILTER` [KGFQ001]

Avoid:

```sparql
?d a ?type .
FILTER(?type = upcore:Disease || ?type = efo:0000651)
```

Prefer:

```sparql
VALUES ?type { upcore:Disease efo:0000651 }
?d a ?type .
```

With one value, put it in the pattern: `?d a upcore:Disease .` The ProKN
disease list went from a timeout after 19,694 requests to 6.8 s and 1,849
requests on this change alone; the information was already in the query, hidden
from source selection by its syntax.

## 2. A constant `BIND` restricts nothing remotely [KGFQ002]

Engines evaluate `BIND` after the pattern it feeds. `BIND(ex:c1 AS ?c) ?c rdfs:label ?l`
scans every label. Write the constant in the pattern, or in `VALUES`.

## 3. Never scan a variable predicate to filter it by name [KGFQ003]

Avoid:

```sparql
?source ?relation ?target .
FILTER(STRSTARTS(STR(?relation), STR(RO:)))
```

`?s ?p ?o` is every triple. Read the predicate inventory instead —
`kgfq schema <dataset> --children properties --all` — pick the IRIs, and write
`VALUES ?relation { RO:0002212 RO:0002213 }`. Six predicates held all 67,267
NCIPID answers; the scan read 597,129 triples to find them.

## 4. Keep the required pattern connected [KGFQ004, KGFQ005]

Every required component must share a variable with the rest. Two components
with nothing in common denote their Cartesian product, and an `OPTIONAL` between
them neither connects nor constrains them:

```sparql
?pert ex:identifier ?id .                # component A
?comp rdfs:label ?label .                # component B
OPTIONAL { ?comp ex:sameAs ?id }         # relates A and B — optionally
```

If the relationship is required, make it required: `?comp ex:sameAs ?id`. The
linter refuses the disconnected form outright; it cannot know which side you
meant.

## 5. `OPTIONAL` is for enrichment, after the selective core [—]

Find the small required set first; add labels, descriptions and geometry
afterwards. A relationship needed to identify an entity or join two components
is never optional. For a large result, prefer two stages: the IRIs from the
selective core, then hydration in `VALUES` batches — which is exactly what
`kgfq fetch --values` does.

## 6. Text filters are not lookups [KGFQ006]

Avoid:

```sparql
?d rdfs:label ?l .
FILTER(CONTAINS(LCASE(?l), "alzheimer"))
```

That scans every label (3.53 M in ProKN). If you know the literal, bind it
exactly. If you do not, get candidates from the text index — `kgfq resolve
alzheimer --datasets prokn` — put them in `VALUES ?d { … }`, and keep the
`FILTER` as the residual check, since the index returns a candidate superset.

## 7. `DISTINCT`, `GROUP BY` and `ORDER BY` read their whole input [KGFQ007]

`SELECT DISTINCT ?label WHERE { ?e ex:label ?label } LIMIT 100` reads all
1.68 M matching triples before it can return 75 distinct values; `LIMIT` cannot
short-circuit a blocking operator. Do not add `DISTINCT` defensively; find the
join that produces the duplicates. If a bounded arbitrary sample is what you
want, say so with an explicit bounded inner stage. If the distinct values are
what you want, fetch the relation to a table and take them in SQL.

## 8. Split large queries into IRI-producing stages [KGFQ008]

There is a measured cliff at about six triple patterns: below it, a stock
fragments engine finished 36 of 38 tasks; above it, 6 of 15. Above the
threshold, decompose:

```
selective IRI query  →  bounded IRI set  →  VALUES-based extension or hydration
```

For federation, resolve a bounded set in one graph and bind those IRIs into the
next. Cross-graph joins use shared IRIs, never labels or computed strings.

## 9. Know which joins multiply before you aggregate [KGFQ009]

Several one-to-many properties in one pattern multiply solution mappings, and
an aggregate then counts the product. Establish which relations are one-to-one,
narrow the required pattern before `GROUP BY`, and aggregate before joining
optional many-valued properties. Or fetch the relations to tables and aggregate
in SQL, where the multiplicity is visible.

## 10. Property paths need an anchor [KGFQ010, KGFQ012]

`?a rdfs:subClassOf* ?b` with neither end bound starts everywhere. Bind one
endpoint, use a fixed-length relation, or use a published closure predicate.
And when a dataset declares a predicate already transitively closed — Ubergraph
materialises `rdfs:subClassOf` — query it directly: every ancestor is a triple,
and the `*` only adds work.

## 11. Project what you need [KGFQ011]

`SELECT *` raises transfer, grouping and distinctness cost and exposes
multiplicity you did not intend. Name the variables.

## What the linter will and will not do

`kgfq lint` parses the algebra and names what the syntax hides. It never
rewrites: the finite-set and constant-bind cases have a mechanical fix, but a
disconnected pattern, an optional bridge, or an aggregation shape have no
recoverable intent, and the decision is yours. Strong diagnostics
(`KGFQ004`, `KGFQ005`) fail the command by default, which is what registry CI
uses to keep a Cartesian product out of the corpus.
