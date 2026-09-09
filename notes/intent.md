# The intent representation — a proposal

Written 2026-09-09, against plan.md C4's open question. Nothing here is built;
phase A's single-pattern `fetch` is the subset it must reduce to.

## 1. What the function has to consume

Every Lane B task, and 39% of the case-study corpus, is a handful of triple
patterns joined on shared variables — some across graphs — with a few of:
`OPTIONAL` patterns (40%), literal filters (49% substring, 28% range), a set
of starting values, and a projection. Aggregation (47%) and `DISTINCT` sit on
top of that retrieval and can run locally once the rows are in a table.

So the thing the strategy function consumes is a **conjunctive retrieval
intent**: patterns, optional patterns, filters, values, projection, budget. The
question is only how an agent writes it down.

## 2. Three ways to write it down

**(a) A small JSON document.** Each pattern names its graph; the three
positions use the doc 03 §3.3 term syntax the CLI already uses; optionals are
a second list; filters are objects with a closed set of operators; values are
inline rows or a reference to a workspace table column.

```json
{
  "kind": "join",
  "patterns": [
    {"dataset": "chemkg", "s": "?c", "p": "ex:affects",   "o": "?g"},
    {"dataset": "genekg", "s": "?g", "p": "ex:inPathway", "o": "?path"}
  ],
  "optional": [
    {"dataset": "genekg", "s": "?g", "p": "rdfs:label", "o": "?label"}
  ],
  "filters": [
    {"var": "?label", "op": "contains", "value": "kinase"}
  ],
  "values": {"table": "chems", "column": "s", "var": "?c"},
  "project": ["?c", "?g", "?path", "?label"],
  "budget": {"max_requests": 200, "max_rows": 1000000}
}
```

On the command line the same thing is repeated flags, and the pattern text is
exactly what `fetch` takes today:

```
kgfq join chemkg '?c ex:affects ?g' genekg '?g ex:inPathway ?path' \
  --optional genekg '?g rdfs:label ?label' \
  --filter '?label contains "kinase"' \
  --values table:chems.s=?c --out chem_pathways
```

**(b) A SPARQL subset**, parsed with rdflib (the linter already parses SPARQL).
`GRAPH <iri>` or a `# dataset:` comment scopes patterns to graphs.

**(c) Single patterns only**, which is phase A: the agent fetches one pattern
at a time, feeds keys forward with `--values`, and joins in SQL.

## 3. Recommendation: (a), with (c) underneath and a bridge from (b)

(a) is the interface. Its content is a BGP, so an agent that knows SPARQL
recognises every part; its structure is what keeps the surface honest:

- **A pattern names its graph.** Cross-graph joins are the point of the
  federation (D14) and the JSON makes the graph of each pattern explicit, where
  SPARQL needs a `GRAPH`/`SERVICE` convention that the case-study corpus shows
  agents getting wrong.
- **Filters are a closed set that maps onto R4.** `=` → bound object, one
  request; `in` → `VALUES`; `contains`/`regex` → `/search` or `o.text` with the
  filter kept as the residual check; `<`, `<=`, `>`, `>=` → fetch and filter
  locally until `/ranges` exists; `lang` → `o.lang`. The linter's KGFQ001,
  002, 003 and 006 become impossible to write.
- **Optionals are a list, not a bridge.** A disconnected required set is
  refused before the first request, which is KGFQ004 and KGFQ005 as planner
  decisions rather than diagnostics.
- **No aggregation, no paths, no `DISTINCT`.** The intent is *what to
  retrieve*, bounded and planned; what to *compute* is SQL over the result
  (D4). Transitive paths are the closed-predicate declaration or `/closure`
  (C4), never expansion.
- **Values can be a table column.** `{"table": "chems", "column": "s"}` is a
  derived input by construction (D17), and it is how a session feeds one
  step's result into the next without the agent copying IRIs into a file.

(c) stays as the escape hatch and as what the skill teaches for decomposition:
a `join` is exactly the automation of what an agent can do by hand with
`fetch --values`, and it must leave the same artifacts behind.

(b) is a bridge, not an interface: `kgfq lint --to-intent query.rq` translates a
query that already fits the subset into the document, for people who have
SPARQL and want the integration contract rather than the complete one. Making
SPARQL text *the* interface would invite the mcp-okn failure mode back in
through the front door — the model writes past the subset, gets rejected, and
the rejection rate becomes the product — and would blur D2 for the model,
which would not understand why "its SPARQL" came back approximate.

## 4. What executing an intent leaves in the workspace

The plan orders the required patterns by count (R1, batch counts for bound
sets), chooses bind-join or column-fetch per step (R2), projects the next
intermediate from the counts in hand before making it (R7), routes filters
(R4), and pushes a limit only when the intent carries one and records it (R3).
Each step **materialises one triple table** in `tables/` with its own receipt —
the same tables `fetch` writes today — and the final join, with `LEFT JOIN`
for each optional and the residual filters applied, is a `derived` table whose
receipt names its inputs. So `cite` works on the result because every
intermediate is a set of triples beside it, and `replay` re-runs the steps.

An intent that the guard refuses produces no table and a plan that says at
which step and by how much the projection exceeded the budget, with the
decomposition it suggests — the Lane A failures as refusals, not timeouts.

## 5. Open for decision

1. The name: `kgfq join` (as in plan §6) or `kgfq query`. `join` says what it
   is; `query` is what people will type.
2. The filter operator set above, or a smaller one for the first version.
3. Whether `project` may be omitted, defaulting to every variable.
4. Whether an intent may carry `limit` at all in the first version, given R3's
   caveat, or whether `--max-rows` on the result is enough.
