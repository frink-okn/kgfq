# kgfq — the KGF client stack

The client half of **Knowledge Graph Fragments**: the library that holds the
strategy function, the `kgfq` command-line client, and the MCP server that agents
use. Python 3.12+, packaged with uv. The server is `../kgf-rs`; the design documents
are `../kgf` (doc 06 is the client design, doc 03 the API); the Comunica engine, its
KGF actor suite, and the complete-answering SPARQL command line are `../kgf-sparql`,
which `kgfq` launches as a subprocess rather than linking.

`notes/plan.md` is the development plan: what the benchmark run established, the
decisions this repository rests on, the unit sequence, how SPARQL is and is not
supported, the evaluation harness, and what the server still owes the client. Phase
A is under way: the endpoint client, discovery and pinning, the workspace with its
log and receipts, and the first `kgfq` commands exist and are tested against two
live `kgf serve` processes.

## Running it

```bash
uv sync
uv run kgfq endpoints add https://apps.okn.us/kgf   # a KGF origin; repeatable
uv run kgfq datasets                                  # every graph on every endpoint
uv run kgfq summary ubergraph                         # the summary card
uv run kgfq count ubergraph '? rdfs:subClassOf ?'      # from the cached inventory, no request
uv run kgfq explain prokn '?g obo:RO_0002200 ?d' --values genes.tsv
uv run kgfq fetch phaseskg '? rdfs:label ?' --out labels   # tables/labels.parquet + receipt
uv run kgfq receipt labels
uv run kgfq log
uv run kgfq lint query.rq                              # KGFQ001–KGFQ012; never rewrites
```

A pattern is three terms in doc 03 §3.3 syntax — `<iri>`, `prefix:local` against the
release's manifest prefixes, `"literal"@en` — with `?` for an anonymous variable and
`?name` for a named one. `--values` takes a JSON `{"vars": [...], "rows": [[...]]}`
document or a tab-separated file whose first line names the variables.

`kgfq lint` parses a query's algebra and names what its syntax hides — a finite set in a
`FILTER`, a variable predicate scanned by name, a disconnected pattern that denotes a
Cartesian product. The rules and the reasoning behind them are in
[docs/sparql-author-guide.md](docs/sparql-author-guide.md).

The working directory is the workspace: `.kgfq/` holds endpoints, pins and caches,
`tables/` the evidence with a receipt beside each file and inside it, and
`kgfq.log.jsonl` the session log.

Tests spawn two real servers over bundles built with `kgf build`; they need the
`kgf` and `hdtc` binaries, found through `KGFQ_KGF` and `KGFQ_HDTC` or the sibling
checkouts' release builds:

```bash
uv run pytest
```

The toolchain is uv, ruff and ty; a change is done when all of these are clean:

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run ty check src tests && uv run pytest
```

```
Source/
  kgf/          # design documents
  kgf-rs/       # the server and build tool (Rust)
  kgf-sparql/   # Comunica engine + KGF actor suite (TypeScript)
  kgfq/         # this repo: the Python client stack — kgfq, kgfq-mcp, kgfq lint
  mcp-okn/      # the pre-KGF MCP server; its benchmark is our acceptance suite
  okn-registry/ # kgs.yaml, prefixes.yaml, and the 89 registry queries
```
