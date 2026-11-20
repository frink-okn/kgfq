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
uv run kgfq list_graphs                               # every graph on every endpoint
uv run kgfq graph_summary ubergraph                   # the summary card
uv run kgfq search ubergraph 'diabetes mellitus'       # a name to its IRIs; the hits are a table
uv run kgfq count ubergraph '? rdfs:subClassOf ?'      # from the cached inventory, no request
uv run kgfq explain prokn '?g obo:RO_0002200 ?d' --values genes.tsv
uv run kgfq distinct scales '? fbi:OffenseUCRCode ?code' --by o   # distinct values with counts; reads no triples
uv run kgfq fetch phaseskg '? rdfs:label ?' --out labels   # tables/labels.parquet + receipt
uv run kgfq receipt labels
uv run kgfq export labels --out labels.nt                # the table's triples, as N-Triples
uv run kgfq log
uv run kgfq lint query.rq                              # KGFQ001–KGFQ012; never rewrites
uv run kgfq sparql query.rq --dataset prokn --out hits   # lint, then kgf-sparql to completion, or a budget
```

A command goes by the name the session format gives its operation
([docs/session/operations.yaml](docs/session/operations.yaml)), which is also the name in
the log. `datasets` and `summary`, the first names of `list_graphs` and `graph_summary`,
still run.

A pattern is three terms in doc 03 §3.3 syntax — `<iri>`, `prefix:local` against the
release's manifest prefixes, `"literal"@en` — with `?` for an anonymous variable and
`?name` for a named one. `--values` takes a JSON `{"vars": [...], "rows": [[...]]}`
document or a tab-separated file whose first line names the variables. Every
variable it names must appear by name in the pattern: a `?paper` column binds
`?paper <…> ?ent`, not `? <…> ?ent`. A column the pattern does not name would bind
nothing, so every row would count or fetch the whole pattern. `count`, `explain`,
`fetch` and `distinct` therefore refuse it with exit 2, naming both sides, before any
request. A partial overlap is refused too (see the plan, C1).

A fetched table's columns are the pattern's variables, four for each: `?zip` is `zip`,
`zip_kind` (`iri`, `literal` or `bnode`), `zip_lang` and `zip_datatype`. A constant has
no column, and an anonymous position's is `s`, `p` or `o`. The receipt keeps the
pattern, so `kgfq export` can put every row back into it and write the triples.

`kgfq search` is how a name becomes an IRI: full-text search over one graph's literals,
labels first. Its hits are a table like any other, so the next step binds from it
(`--bind search_atrazine:subject`) and an IRI taken from it is recorded as derived from
that step, where one typed from memory is recorded as asserted.

`kgfq labels <table> <column>` writes a table of one label per IRI in a column, to join
when names have to be part of the answer. The graphs are asked in order (`--graph`,
repeatable) and the first label found wins; the default is the graph the table was read
from, which is often not where the names are, so the handle says how many IRIs no graph
labeled, with a sample.

`fetch` counts a pattern before reading it, and refuses one over 1,000,000 rows before any
row is read, saying how large it is, about how long it would take, and what would make it
smaller. `--max-rows N` takes a part, `--all` reads it whole, and `KGFQ_FETCH_GUARD_ROWS`
moves the limit (`off` removes it).

`--bind` carries one step's result into the next without a file between them:
`--bind affected:gene` gives the pattern's `?gene` the distinct values of the table
`affected`'s column `gene`, and `--bind chems:c=chem` binds `?c` to the column `chem`. The
terms are read back from the table, kind, language and datatype included, so a join across
two graphs is a fetch from one and a bound fetch from the other. `fetch`, `count`,
`distinct` and `explain` take it, in place of `--values`.

`kgfq distinct` lists the distinct values one position of a pattern takes, each with
its triples, through doc 03 §3.4.13's `/distinct`. The server walks its indexes, so the
cost follows the number of values, not the triples behind them. `--by` names the
position, and every position of every pattern is answered. With `--values` the values
merge over every row, and each also says how many rows reached it. A server that
predates the operation publishes no `distinct` link, and the command says so before any
request.

`kgfq lint` parses a query's algebra and names what its syntax hides — a finite set in a
`FILTER`, a variable predicate scanned by name, a disconnected pattern that denotes a
Cartesian product. The rules and the reasoning behind them are in
[docs/sparql-author-guide.md](docs/sparql-author-guide.md).

`kgfq sparql` is the complete-answering path (plan §5): it lints the file, resolves
each `--dataset` (or the file's `#+ tags`) to a pinned release, launches `kgf-sparql`
with a budget (20,000 requests, 256 MiB, 60 s by default) and a receipt path, and
files the result as a table with a receipt of kind `sparql`. Exit 0 is exactly the
solution set; 3 is a run aborted at a named budget, with its receipt; 1 is the
engine's error, verbatim. The engine's own receipt, every request with its id, is
filed beside the table as `tables/<name>.kgf-sparql.receipt.json`. `--normalize`
applies the two proven-equivalent rewrites (KGFQ001, KGFQ002) and records each. The
engine is found through `KGFQ_KGF_SPARQL` or `PATH`; `npm install -g kgf-sparql`, or
point the variable at `../kgf-sparql/packages/kgf-sparql/bin/kgf-sparql.js`.

The working directory is the workspace: `.kgfq/` holds endpoints, pins and caches,
`tables/` the evidence with a receipt beside each file and inside it, and
`kgfq.log.jsonl` the session log.

Tests spawn two real servers over bundles built with `kgf build`; they need the
`kgf` and `hdtc` binaries, found through `KGFQ_KGF` and `KGFQ_HDTC` or the sibling
checkouts' release builds, and the `kgfq sparql` tests need `kgf-sparql` (the sibling
checkout's built package and `node`, or `KGFQ_KGF_SPARQL`), skipping otherwise:

```bash
uv run pytest
```

The toolchain is uv, ruff and ty; a change is done when all of these are clean:

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run ty check src tests && uv run pytest
```

`bench/registry_corpus.py` runs the okn-registry corpus through `kgfq sparql` against
a live server and compares the result with the kgf-sparql corpus run kept in
`../kgf-sparql-bench`; its summaries are kept as `bench/registry-corpus-<date>.json`.

```
Source/
  kgf/          # design documents
  kgf-rs/       # the server and build tool (Rust)
  kgf-sparql/   # Comunica engine + KGF actor suite (TypeScript)
  kgfq/         # this repo: the Python client stack — kgfq, kgfq-mcp, kgfq lint
  mcp-okn/      # the pre-KGF MCP server; its benchmark is our acceptance suite
  okn-registry/ # kgs.yaml, prefixes.yaml, and the 89 registry queries
```
