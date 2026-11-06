# kgfq — development plan

Written 2026-09-03 against `../kgf` docs `970d442`, `../kgf-rs` `2e2e26c`,
`../kgf-sparql` `7173d9c`, `../mcp-okn` `4e49e2c`. Nothing here is built.
Revised the same day: **D1 changed from TypeScript to Python** (§2, with the
reasoning kept), and `../kgf-rs` landed request logging as its unit 22 at `c10b325`
while this was being written, so §10 records it as done. Revised 2026-09-09 after
the first design discussion: **D13–D17** (local-only as a principle, the graph rather
than the server as the unit, the workspace as the product, SPARQL-only sources by
template, source grades and the import door), units **C9–C11** for provenance, and
the §5–§9, §11 and §12 changes that follow; the intent representation (C4) is the
open question that remains. This is the
route, the decisions it rests on, and the questions it raises for the design docs;
keep it current the way `../kgf-rs/notes/plan.md` is kept, and record what each unit
decided under its heading when it lands.

The one-sentence brief comes from the benchmark
(`../kgf/docs/benchmark-implications.md` §1): **the protocol is finished enough, and
the client is the part of the system that has not been built.** The same eight tasks
cost 86,222 requests executed naively and 194 planned well; fifteen tasks never
finished and every one of those failures was in the client. This repository is that
client.

## 0. What this repository is, and what it is not

Doc 06 §6.2 names four client deliverables: a **library** holding the strategy
function, an **MCP server**, an **agent skill**, and a **CLI**. The implications
document's §2 adds the rule that binds them: the MCP server and `kgfq` are thin skins
that *share* the strategy function, because a planning rule that exists twice will
diverge and the measured cost of divergence is three to four orders of magnitude.
So one repository, one library, several skins:

| module / entry point | role |
|---|---|
| `kgfq.client` | the library: discovery, endpoint client, statistics, executors, receipts |
| `kgfq.plan` | the strategy function and the guard — the one place operation selection lives |
| `kgfq.cli`, console script `kgfq` | the CLI — the intent surface as shell commands, `explain`, `lint`, a marked raw mode, and a thin `sparql` wrapper |
| `kgfq.mcp`, console script `kgfq-mcp` | the MCP server — the intent tools over the library and a sandboxed local DuckDB |
| `kgfq.lint`, also `kgfq lint` | the SPARQL linter (`KGFQ001`–`KGFQ012`), usable standalone and by registry CI |
| `skills/` | the agent skill: the loop, the count-before-fetch habit, reading cards |
| `bench/` | the evaluation harness, a first-class deliverable (§8) |

One Python distribution, `kgfq`, because the strategy function must exist once and
every skin must import the same copy of it; the linter and the MCP server are
modules with their own entry points rather than separate packages.

And one deliverable that is not code: the **workspace** (D15, C9) — the directory an
analysis leaves behind, holding its tables, their receipts, and the log that can
re-run it. It is what the user keeps.

**Not here:** `../kgf-sparql`. It is the Comunica engine plus the KGF actor suite —
and, after §5.2's additions, the complete-answering SPARQL command line. It versions
with Comunica rather than with the KGF API, and its actors are the community-facing
artifact doc 06 §6.4 calls "a collaboration with that community." This repository
launches it as a subprocess (§5) and contributes to it; it does not link it and does
not absorb it.

**Also not here:** anything server-side. Where a client rule wants a server change,
§10 lists it and `../kgf-rs` builds it.

## 1. What the benchmark established — the specification

Everything in this plan is a response to a measurement. The measurements, so the
plan can be checked against them rather than against taste:

- **Coverage is not the constraint.** 67 of 68 benchmark tasks are served by
  operations that already answer; none needed `/star`, `/ranges`, `/export`, or
  `/closure` (results §2).
- **The server is never the bottleneck.** ~5,000 req/s, p50 2.6 ms, 3.54 GB resident
  for 6.7 B triples, zero server errors across 807,695 requests (results §5).
- **Planning failures live in the client and are structural.** Comunica receives exact
  `hydra:totalItems` on every fragment and still planned 157,650 requests for one
  query, because its join-selectivity estimate is a pure function of the query text
  that no server response can reach (guidance §3a). Replacing that actor with one fed
  from published VoID rescues the catastrophic tail — 26,023 → 721 requests, an OOM
  → 5.9 s — and leaves the median task untouched: **1.45× total, median ratio 1.000,
  zero wrong answers in 180 executions** (guidance §5.6). It is a tail fix, not a
  speedup.
- **Two configuration defaults cost more than any plan choice.** Comunica's bind
  batch is 16 where KGF accepts 1,000 (~62× on every bind join), and it never sends a
  `limit`, so every scan pages at 100 where 10,000 is allowed (100× on every scan;
  14,895 of one query's 14,927 requests) (guidance §1a, §5.1a). GET `values=` also
  overflows the URL on long IRIs; bindings belong in a body (client-suite §6.1).
- **Heap, not wire, kills heavy queries.** Five tasks exhausted an 8 GB heap having
  received 68–514 MB — 16× to 121× amplification from a streaming-iterator engine
  holding intermediates in JavaScript objects (implications §3.8).
- **There is a cliff at six patterns.** At six or fewer, stock Comunica over brTPF
  finished 36 of 38 tasks with a median of 111 requests; above six, 6 of 15. The
  discriminator is whether the engine happens to start from a selective step
  (results §3.1).
- **Real research queries are harder than the benchmark.** 468 case-study queries:
  49% substring filters, 47% aggregation, 40% `OPTIONAL`, 28% range filters, 39%
  cross-graph. All 39 transitive paths are `rdfs:subClassOf*` against Ubergraph, which
  has already materialized that closure (results §9).
- **Syntax hides selectivity.** On the 89 registry queries, replacing a case-folded
  label filter with the exact literal took one query from 14,593 to 384 requests; a
  namespace filter over `?p` replaced by six `VALUES` predicates from `/schema` took
  another from a timeout to 701. One query denoted an accidental Cartesian product
  (`../kgf-sparql-bench/docs/okn-registry-query-evaluation.md`).
- **The instrument matters.** `mcp-okn` sent 86% of its traffic as model-authored
  SPARQL through a passthrough tool, and the graph whose docstring taught
  `subClassOf*` had the worst failure rate (doc 12 §12.5.3). The MCP transcript
  under-reported endpoint traffic by 1.8× (§12.5.4).

## 2. Decisions

Each is a design decision, to be surfaced rather than quietly changed.

**D1. Python, on 3.12 or later, packaged with uv.** The core of this repository is
the strategy function, DuckDB materialization, receipts, and the MCP server, and
Python is the best of the three candidates at all four: DuckDB's Python binding is
the reference one (relations, Arrow, Parquet and Polars interop, `read_json_auto`);
the MCP SDK is first-party; `mcp-okn`, its benchmark, and the registry-corpus runner
in `../kgf-sparql-bench/test` are already Python, so the harness and the collaborators'
code are continuous with this; and a heuristic planner that will change weekly as
the benchmark teaches wants the fastest iteration loop available. Distribution is
`uv tool install kgfq` or `uvx kgfq`. Dependencies: `httpx`, `duckdb`, `pyarrow`,
`mcp`, `rdflib` (the linter's algebra), `pyyaml`. *Added 2026-09-09:* the toolchain is
uv, ruff and ty — uv owns the environment and lock, ruff lints and formats, ty
type-checks — and all three plus pytest must be clean before a change is done
(`CLAUDE.md`).

*The first draft chose TypeScript, and the reasoning is kept because it was wrong in
an instructive way.* It coupled the whole stack's language to the SPARQL path, which
this same plan calls secondary, not an MCP tool, and a tail fix with a known low
ceiling; and it claimed the SPARQL path had to run in-process to be budgeted, which
is overstated — the budget, the statistics supply, and the receipt belong in
`kgf-sparql`'s own CLI (§5.2), and any language can launch it and read the receipt
back. What in-process Comunica actually bought was using the engine as the executor
for small plans, and a small conjunctive plan is a bind-join loop of about twenty
lines in the library (§5.5); the Lane B plans that produced the six rules were
Python scripts. Against that, TypeScript's costs were real: a CommonJS Comunica under
ESM tooling, a cold `npx` pulling hundreds of packages into an MCP server that does
not use them, and a DuckDB binding younger than Python's.

**Rust was the other candidate**, and doc 07 §7.5 item 25 already anticipated
`kgfq` as a later crate in the `kgf-rs` workspace: one static binary is the best
distribution story of the three, `spargebra` is the best SPARQL algebra, and a client
sharing the server's term-syntax and envelope modules could never disagree with it
about the wire. Not chosen now because the planner is heuristics that must be cheap
to change, and a language with ten-minute bundled-DuckDB builds is the wrong place to
iterate them. Revisit if distribution friction ever dominates, or once the rules
have stopped moving.

**D2. Two contracts, two surfaces, never blurred** (implications §1b). *Agent
integration* answers an intent under a budget, may approximate, must say so in a
receipt, and never fails — it degrades. *Complete answering* returns exactly the
solution set a SPARQL query denotes, may fail, and never approximates. Every MCP tool
and every `kgfq` command is the first contract. The second contract is
`kgf-sparql`, a separate tool in a separate repository; `kgfq sparql` only launches
it and files its receipt (§5.2), and there is no MCP tool for it (§5.4).

**D3. Operation selection is deterministic code using published statistics**
(doc 06 §6.1, sharpened by the benchmark). Not in prompts, not in docstrings, and not
delegated to a general engine, because a general engine given exact cardinalities still
produced the 157,650-request plan. The strategy function is one module in
`kgfq.plan`, it emits an inspectable plan, and `kgfq explain` prints it.

**D4. Materialize and spill for anything the guard calls heavy** (implications §3.8).
Small plans stream; predicted-large plans fetch columns into DuckDB and join there.
`OPTIONAL` is a `LEFT JOIN`, `DISTINCT` and `GROUP BY` are native, and a 500 MB
intermediate is a table rather than a heap.

**D5. Version pinning and receipts on everything** (doc 06 §6.2.1). `latest` is
resolved once per session and pinned; every table and every plan carries dataset IRI,
release label, `content_digest`, the operation sequence, request count, bytes,
completeness, every approximation made, for a bound fetch how many of the values it was
given found nothing (C6), and the server's `KGF-Request-Id`s once the
server sends them (`../kgf-rs/notes/request-logging.md` §4). A receipt is what makes
an approximate answer a finding rather than a bug.

**D6. The DuckDB is sandboxed as doc 06 §6.7 specifies.** `enable_external_access =
false`, extension autoinstall and autoload off, a dedicated temporary directory, table
and column identifiers constructed by code and never interpolated from model output
or graph content, and an allowlist of statement kinds for the `sql` tool. Publisher
prose is the publisher's and is trusted — a KG we query is a curated source, and
distrusting its prose would mean trusting nothing it says (settled 2026-09-09; the
server's summary card carries the description bare on purpose, and doc 06 §6.7,
doc 04 §4.2 and doc 07 §7.4 were refined the same day to say so).
The sandbox is not about the publisher. It is least privilege: literals can carry
third-party text (abstracts, records, scraped descriptions), an agent holding a
`sql` tool and a shell can be steered by text it reads, and removing the
capabilities that would make that harmful — no external access from DuckDB, no
identifier built from content, no rows into context, `import` as the one door with
the library fetching URLs — costs nothing and needs no label on the data. The
evaluation (§8) keeps the scenarios that test those mechanisms.

**D7. Capabilities are checked, not believed.** Every manifest today declares `star`,
`terms`, and `export`, and all three 404 (results §7.1). Until the server's
declarations are truthful, the library probes an operation once per release and caches
the answer beside the manifest, and the planner chooses fallbacks by *verified*
availability.

**D8. JSON transport now, isolated so Parquet is a one-module change.** Parquet is
specified (doc 06 §6.3) and unimplemented (`kgf-rs` has it only in tests asserting
it is unsupported). The transport layer maps a KGF response to Arrow-shaped columns
`s, p, o, o_kind, o_lang, o_datatype`; when the server ships Parquet the same columns
arrive faster and nothing above the transport changes — once doc 06 §6.3 says how its
Parquet tells an IRI from a plain literal (§11 item 12). `o_kind` was added
2026-09-24; see C6.

**D9. Bindings go in a QUERY body with POST as the fallback**, chunked by *both*
`max_bindings` and `max_request_bytes` (client-suite §6.1). Detect QUERY support from
`Accept-Query` on the first response and remember it per origin.

**D10. The evaluation harness is a deliverable, not a chore.** The benchmark is the
specification; the harness is how a change is shown to honor it (§8). The doc 06 §6.1
ablation — identical tasks through intent tools and through raw operations — cannot
run without this repository, and it is the central unproven claim of the design
(results §10.2).

**D11. `User-Agent: kgf-client/<version> (kgfq|mcp)`** on every request, so the
server's access log can classify this client without fingerprinting
(request-logging note §4).

**D12. The plan format is a documented artifact.** The strategy function's output —
steps, chosen operations, estimated requests, bytes and rows, completeness
expectations, approximations — is a JSON document with a schema in `docs/`, because
`explain` prints it, receipts embed it, and the harness diffs it. Two plans that differ
only in a rule's decision should diff in one place.

**D13. Local-only, as a principle rather than a phase.** The client runs on the
user's machine: the CLI as a process in the working tree, the MCP server over stdio.
No hosted variant is a goal, in any release. A server running joins on behalf of its
users is the federated endpoint's cost structure again — the structure KGF exists to
escape — and doc 06 §6.2's "accepting that it pays for their joins" understates it.
Browser-only chat clients are out of scope; Claude Desktop is reached through stdio.
The session is still an explicit object threaded through the library rather than
module state, for tests that spawn isolated sessions and for parallel agents that
must not share pins by accident — not for hosting.

**D14. The unit is a graph at a pinned release; the server is incidental.** The
target topology is doc 05 §5.1's: providers run their own KGF servers, and one
central deployment serving forty graphs is today's instance, not an assumption. A
session holds graphs. Each graph's endpoint is a property of the graph, read from its
registry entry or given explicitly, and a graph in no registry works. Caps, QUERY
support (D9), capability probes (D7), rate limits and request ids are per endpoint,
because each provider publishes its own, and a cross-graph join reads two caps
tables. Identity is the release's content digest; the origin is recorded as where it
was fetched. Nothing in the CLI, the workspace or the MCP surface groups by server,
and two graphs on one server are not thereby worth querying together: joinability
comes from shared identifier spaces — R6's precondition — never from co-hosting.
Capability heterogeneity is the normal case, since some provider will run an older
server, so the fallbacks D7 chooses are the design rather than a workaround for
today's 404s. Golden tests spawn two servers serving different graphs and join
across them.

**D15. The workspace directory is the product, and the session log is a script.**
An analysis ends as a directory of evidence: one Parquet file per fetched or derived
table with its receipt beside it as a sidecar and inside it as footer metadata, and
an append-only log in which every library operation is one record (C9). DuckDB is
the engine over that directory and never the store: it opens in-memory with a view
per table, and its file is scratch for spills. Files rather than one database
because DuckDB admits one writer process per file, and parallel subagents or a script
running several `kgfq` commands would contend for it (`../mcp-okn` met the
parallel-subagent case on its query log); because a receipt inside the file cannot be
separated from the rows; and because `ls` is the audit tool everyone already has.
What makes the log a script is a protocol property, not a client one: releases are
immutable and digested, fragments and schema pages enumerate in the published HDT
order, `/sample` is deterministic per seed and version, and search ranks over an
immutable index (doc 03), so every operation is a pure function of the pinned release
and the request — on any server any provider runs. C10 replays it; C11 turns it into
citations and a report. The library writes the log, not the model: `mcp-okn`'s record
depended on the model flagging exploratory queries and not pasting looked-up
constants, and doc 12 §12.5.4 measured the transcript under-reporting endpoint
traffic by 1.8× as the result.

**D16. SPARQL-only sources are reached by the strategy function through templates
that are code's, never by SPARQL the model wrote.** KGF composes with graphs whose
only service is `services.sparql` — a tier-2 endpoint without a bundle, or an
affiliate with steward-built descriptors (doc 05 §5.0, §5.6) — and doc 05 §5.6.4
already says how: the strategy function compiles intents to bounded SPARQL
templates. The first draft of this plan dropped that path (§5.5); this restores it
as a third executor (C5). The intent surface is unchanged — `count`, `fetch` with
bindings as `VALUES`, `labels`, `resolve` — and the templates are a fixed, tested set
in `kgfq.plan`, so D3 holds and §5.4's passthrough tool stays unbuilt. What the
executor cannot promise it declares: `OFFSET` paging is unstable if the graph
mutates, `sample` has no seed, completeness is unknown unless a count agrees, and
every request is best-effort under a timeout and a row budget. Schema and statistics
come from the descriptor artifacts, self-reported, as doc 05 §5.6.3 labels them. A
graph with both a bundle and a SPARQL service is served from the bundle; its SPARQL
service is used only where §5.7 already allows, on explicit request, with the
receipt naming the substrate.

**D17. Every table carries a source grade, and the only door into the workspace is a
logged step.** Four grades, by three questions — can it be re-fetched identically,
can a row be pointed at in an immutable source, and who described the origin:

| grade | re-fetch identically | point at a row in an immutable source | origin described by |
|---|---|---|---|
| `bundle` | yes, by digest | yes — a triple at a release | the protocol |
| `sparql` | attempted and compared; may differ | no — the archived result plus the query | the registry or steward |
| `external` | from the archive only | no — the import step | the agent or user |
| `asserted` | no | no | the agent or user, unnamed |

A derived table's grade is the minimum over its inputs — one hop per table, never
lineage through SQL — so the report (C11) can put a grade beside every number and
name the step where the weakest input entered the data flow. Data from any other
source — another MCP server's result, a file, an API — enters through `kgfq import`
(C9), which records a caller-supplied description the tool cannot verify and says
so, a content hash it can, and the URL, time and `ETag` when it fetched the data
itself. That is the D6 sandbox read as a provenance rule: the DuckDB reads no file
the library did not write, so nothing reaches a join without a receipt.

## 3. Architecture

```
  coding agent            human / script            existing .rq corpora
       │                        │                          │
       ▼                        ▼                          ▼
    kgfq-mcp                  kgfq                     kgf-sparql
   (intent tools)         (intent commands)     (../kgf-sparql, Node —
       │                        │                complete answering, §5)
       └──────────┬─────────────┘                          │
                  ▼                                        │
      kgfq.client · kgfq.plan · kgfq.lint                  │  Comunica + the KGF
   ┌────────────────────────────────┐   kgfq sparql        │  actor suite; its own
   │ discovery · endpoint client    │ ──(subprocess)──────►│  statistics supply,
   │ statistics · strategy function │                      │  budget, and receipt
   │ guard · executors · receipts   │                      │
   └───────────────┬────────────────┘                      │
                   ▼                                        ▼
            KGF HTTP API  ◄─────────────────────── (brTPF / native source)
```

The library never links the engine — there is nothing to link. `kgfq sparql` runs
the linter, launches `kgf-sparql`, waits, and reads its receipt back; that process
boundary is the contract boundary (§5.5).

Layout, one uv project:

```
kgfq/
  pyproject.toml          one distribution: kgfq; console scripts kgfq, kgfq-mcp
  src/kgfq/
    client/               endpoint client, discovery, statistics, executors, receipts
    plan/                 the strategy function and the guard
    lint/                 the SPARQL linter
    sparql/               launching kgf-sparql, its results and receipt, --normalize (§5.2)
    mcp/                  the MCP server
    cli/                  the CLI
  skills/kgf-integration/ the agent skill
  bench/                  harness, corpora pins, frozen baselines (registry_corpus.py and its runs)
  docs/                   plan, receipt, intent and log-record schemas, author guide
  notes/                  this file
```

## 4. `kgfq.client` and `kgfq.plan` — units

Each unit says what it decides and what verifies it. Golden tests spawn two real
`kgf serve` processes from `../kgf-rs`, over hdtc-built fixture bundles for different
graphs, the way `kgf-rs` tests spawn `hdtc` (D14); a `KGFQ_KGF` variable names the
binary. No mocked server: the traps this
library exists to absorb were all found against a live one.

### C1. Endpoint client

Service descriptor, dataset descriptor, manifest; the caps and budgets a request is
read against; doc 03 §3.3 term syntax in both directions; cursor paging that is
transparent to callers; bindings chunking (D9); QUERY with POST fallback;
`Accept: application/json`; the `User-Agent` of D11. Every one of those is a
property of the graph's endpoint (D14): caps, QUERY support and request ids are held
per endpoint, and the client carries no federation-wide constant.

Three traps it must absorb, each of which cost real time in the benchmark
(implications §4): bindings rows are keyed **positionally** (`s`/`p`/`o`) with a
**0-based `binding` index relative to the submitted chunk**, not the caller's
variable names; a chunk can span several cursor pages and the index stays
chunk-relative, so advancing per page silently drops rows; and `/search`'s
`complete: true` means the bounded top-k window finished, not that every match was
returned (`../kgf-rs/notes/search-completeness.md`, resolved in doc 03 as a bounded
window). The library re-keys by the caller's variables, owns the chunk arithmetic, and
exposes search completeness under a name that cannot be misread.

*Verified by:* every pattern shape paged at adversarial page sizes against `/count`;
a bindings request whose chunk spans pages, compared row-for-row with the
unrestricted fragment; QUERY refused by a proxy and POST taking over.

*Landed 2026-09-09* as `kgfq.client.endpoint` (with `terms` and `errors`), tested
against two live `kgf serve` processes (`tests/test_endpoint.py`). Decided while
building: an observed QUERY refusal outranks the origin's `Accept-Query` header,
since the path to an origin is what refuses; redirects are followed by hand with
`follow_redirects` off so a `latest` 307 never rewrites a body-carrying method;
`rate_limited` and `internal_error` retry up to three times, nothing else does;
chunking halves the row count until a body fits `max_request_bytes`; rows come back
keyed by the caller's names with the ``binding`` index made absolute; every request
is a `RequestRecord` with the server's id, wire and decoded bytes, and completeness
read from the `KGF-*` headers before the body.

*Added later the same day*, after the first run against `https://apps.okn.us/kgf`:
descriptor links are root-relative paths that already carry the deployment's
public-base prefix (`/kgf/ubergraph/v/…`, doc 03 §3.2), so they resolve against the
host, while the two paths the client spells itself — the service descriptor and a
dataset by id — live under the origin, prefix included. Getting that wrong made the
live server look empty. Both gateway behaviours now have tests behind a small
in-test reverse proxy (`tests/proxy.py`): links behind a `--public-base` prefix
resolve, and QUERY refused with 405 by the proxy falls back to POST and stays
there for that origin. The live gateway passes QUERY through. §10 item 3's client
half is therefore done.

*Added 2026-11-06 — bindings name pattern variables.* The server joins a bindings
block the way SPARQL joins `VALUES`. A column that shares no variable with the pattern
binds nothing, and each row answers the whole pattern. In the pilot benchmark's
Haiku dry run (`../kgf-pilot-benchmark`, `runs/dry-20261106/K/claude-haiku-5-5/
climatemodelskg__v3_14`), an 891-row `?paper` file against
`? <https://climatepub4kg.github.io/ontology#PAPER_MENTIONS> ?ent` gave a count of 457,693,335 (891 × 513,685, the
unbound predicate). The matching fetch pulled the predicate once per row, and the
harness killed the run after more than ten minutes. `Pattern.check_bindings` now
refuses every binding variable the pattern does not name. It names the missing
variables and the pattern's own, and suggests the pattern with the anonymous position
named. It runs at every door: `plan_fetch` before R1's count (so `explain` and
`fetch` refuse on either R2 route), `Statistics.count_bindings` before the cache is
read, and `Endpoint.bindings`/`count_bindings` before a chunk is sent. A bare `?` is
not a name, although the wire spells it `?s`/`?p`/`?o`; a `?s` header against
`? p ?o` used to bind by that accident, and is refused with the rest.

**Partial overlap is an error, not a defined case.** A `?c ?x` file against
`?c ex:affects ?g` was well defined on the wire, where `?x` rides along unbound. It was
not well defined in kgfq: on the fixture, the bind-join route returned six rows, two
per row of input, repeating the rows that differ only in `?x`, while the
column-fetch route looked `?x` up in each fetched triple, found nothing, and kept no
row. The answer depended on R2's cost rule. Making the routes agree would mean
carrying unbound columns through a table whose columns are fixed (C5, C6). Refusing
loses nothing, because the `binding` column indexes the input rows and an extra
column joins back in DuckDB. The likelier cause is a mistyped header, and one rule
(every name in the file appears in the pattern) is one an agent learns from a
single refusal. *Verified by:* `tests/test_fetch.py`, where every door refuses the
dry run's shape, the wire-name case and a partial overlap, with no request and no
cache entry; and `tests/test_cli.py`, where `count`, `explain` and `fetch` exit 2,
with nothing logged and no table written.

### C2. Discovery and pinning

A session is a set of graphs (D14). The workspace's endpoint list is seeded from the
registry's `kgs.yaml` (doc 16 §16.1) and its `kgf:` endpoint field once it exists
(deployment plan §4), extended by `kgfq endpoints add <url>`, and a graph in no
registry is a first-class case (doc 05 §5.6). `find_datasets` joins the seed list to
each graph's service and dataset descriptors and never groups by server. A dataset
argument — a registry shortname, a dataset IRI, or an explicit endpoint — resolves to
one graph at one pinned release; a shortname that resolves to two different digests
is an error naming both, never a silent pick. Resolve `latest` to a release once per
session and pin (D5). Probe declared capabilities once per release and per endpoint
(D7), since provider-run servers will differ.

*Verified by:* a session that resolves `latest`, then sees a new version published,
and keeps answering against the pinned one; two fixture servers, one graph each,
discovered, pinned and probed independently.

*Landed 2026-09-09* as `kgfq.client.session` (`tests/test_session.py`). A dataset
argument is an id, a dataset IRI, or `id@origin`; the service descriptor's entries
carry no dataset IRI, so resolution by IRI reads dataset descriptors. Pins live in
`.kgfq/pins.json` and a pinned dataset resolves with zero requests. The same id at
two origins with different digests raises `AmbiguousDataset` naming both. D7
probes: a capability whose route answers anything but 404/501 is available; one
with no link is not; `filters` and `keysets` are verified from the manifest's
artifact list; verdicts are cached per digest under `.kgfq/cache/capabilities/`.
The registry seed list is not read yet — endpoints are explicit until `kgs.yaml`
carries a `kgf:` field.

### C3. Statistics

`/summary`, `/schema` with `children=properties` (triples, `distinctSubjects`,
`distinctObjects` per predicate — published since August, doc 07 §7.5 item 29),
`projection=class-properties` and `class-relations`, and `/count` in both its
single-pattern and **batch bindings** forms — the planner's workhorse, one round trip
sizing an entire candidate set. All cached per release forever, because releases are
immutable.

*Landed 2026-09-09* as `kgfq.client.statistics` (`tests/test_statistics.py`), cached
under `.kgfq/cache/stats/<digest>/`: the root node, the `properties` and `classes`
inventories in the server's own item shape, the two projections keyed by their
filters, and `counts.jsonl` for single and per-binding counts. Decided while
building: numbers come from the **queryable** view, whose counts doc 03 §3.4.10
makes equal to `/count`'s, and a test asserts that equality for every predicate.
So an exact inventory number answers a count with zero requests — `? p ?`,
`? rdf:type C`, `? ? ?` — which is R1's "nearly free" made literal: on the live
server, ubergraph's 112 M `rdfs:subClassOf` count costs one inventory fetch
(two pages of 1,266 predicates) and nothing afterwards. One more bound position
gets a VoID-style average (`triples / distinct_subjects`) marked inexact, for R1
ordering before any count; anything else goes to the server once. Batch counts
send only the rows the cache has not seen. The planner reports the source of
its count in the plan's evidence (`inventory:predicate`, `cache`, `server`), and
`kgfq schema --all --children properties|classes` reads the cached inventory.

*Fixed 2026-11-06 — a per-binding count is keyed by the request it stands for.* The
key was the bound positions, the bound variables and the row. It never said which
position a variable held, so `?c p ?g` and `?g p ?c` under one `?c` file shared an
entry. On the fixture, the subject binding's 2 answered for the object binding, whose
count is 0. The same gap let the dry run's mistaken call (C1, 2026-11-06) poison the
corrected one: after the bare-`?` pattern with a `?paper` file had cached 513,685 per
row, the same predicate with its subject named `?paper` read those entries back instead
of its real count of 195. That run's `counts.jsonl` holds 891 of them among 906.
`canonical_bindings` now keys on the pattern as the bindings body sends it, every
position with its variable named. Entries under the old key are never read again,
since the new key always spells all three positions. A single count keeps
`canonical_pattern`, because a single `/count` sends only bound positions.
*Verified by:* `tests/test_statistics.py`, where the subject and object bindings get
their own counts and their own cache entries, and an entry planted under the old key
is not read.

### C4. The strategy function

Six rules drawn from plans written by hand and measured (implications §3), plus the
guard. The plan is a data structure (D12), built before any data request is made.

- **R1 — order by selectivity, cheaply.** `/count` every pattern; batch counts for
  bound candidate sets; `class-properties` for an estimate before any count at all.
- **R2 — bind-join versus column-fetch by a computable rule.** With `k` distinct join
  keys and pattern cardinality `c`: bind-join costs `⌈k / max_bindings⌉` requests,
  column-fetch `⌈c / max_limit⌉`. At the published caps the crossover is `k < c/10`.
  Derived from the caps the descriptor publishes, never hard-coded.
- **R3 — push the limit to the most selective pattern**, and record in the receipt
  that this changes *which* rows, not how many — legal without `ORDER BY`, and an
  approximation the complete-answering contract may not make (§5.4).
- **R4 — route literal constraints.** Exact literal → bound-object `/fragment`, one
  request. Substring, regex, fuzzy → `/search` or `o.text`, with the original filter
  kept as a residual check when the operation returns a candidate superset. Numeric
  and date ranges → fetch and filter locally, since `/ranges` does not exist.
- **R5 — constraints on the predicate read the inventory.** `?s ?p ?o` filtered by
  predicate prefix is unbounded in SPARQL; in KGF it is one `/schema` page and one
  bounded fragment per matching predicate (12 requests for 67,267 rows, against
  5,999 and 46 s).
- **R6 — size a cross-graph join before making it**, from the `key_count` headers of
  the two sides' sketches and key sets: within ~1:1,000 pair the sketches; lopsided,
  probe the small side's keys against the large side; both large, accept the bounded
  residual. `sharedKeys(small, large)` is one client operation with two
  implementations behind it — `QUERY /terms` with a key list when the server routes
  it, bindings `/count` with IRIs today — chosen by verified capability (D7). And the
  precondition no estimate catches: do the two graphs share an identifier space in the
  joined roles? Empty looks like a valid answer. `stats/namespaces.json` answers it and
  has no route yet (§10); until then, the manifest's prefix maps are the proxy.
- **R7 — the guard.** Before each join step, project the intermediate size from
  the counts in hand. Over the client budget: reorder, switch to column-fetch and
  DuckDB (D4), or stop and say so. All fifteen Lane A failures were one shape — a
  middle step that exploded while every request stayed inside its cap. Nothing
  server-side bounds a *plan*; this is the single most valuable thing the function
  does.
- **Closed predicates.** When the dataset descriptor declares a predicate as already
  transitively closed (doc 14 §14.4, not yet in doc 03), never expand it. Every
  transitive path in the case-study corpus was this one mistake against Ubergraph.

*Verified by:* the eight hand-written Lane B plans (results §4) reproduced by the
function from the reference SPARQL's intent, within a small factor of their request
counts; the guard refusing each of the fifteen Lane A failures before the first data
request.

*Open — the intent representation.* This unit's output is specified (D12); its input
is not. `fetch(ds, pattern[, bindings])` is clear. What an agent hands the function
for a multi-pattern task with `OPTIONAL` and filters — the shape of every Lane B
task — is not, and if the agent writes the plan, the agent is selecting operations,
which D3 forbids. To settle before B: a small intent document (patterns, optional
patterns, filters, projection, budget) with a schema beside the plan's in `docs/`,
with single-pattern `fetch` plus SQL composition as the phase-A subset.

### C5. Executors

Streaming for plans the guard calls small: rows in memory, joined in code. Materialize
for heavy plans: one table per fetched pattern with columns `s, p, o, o_kind, o_lang,
o_datatype` (doc 06 §6.3, plus `o_kind`), written as Parquet into the workspace (D15) and joined in
DuckDB — `LEFT JOIN` for `OPTIONAL`, `DISTINCT`, grouping and sorting in SQL. Plain IRIs in the tables — HDT dictionary ids are per-bundle and
cannot survive a cross-graph join (implications §3.8), and DuckDB dictionary-encodes
strings well anyway.

A third executor, for SPARQL-only graphs (D16): the intent compiled to one of a fixed
set of templates — `count` to `SELECT (COUNT(*) AS ?n)`, `fetch` with bindings to a
`VALUES` block, `labels` to a batched label pattern, `resolve` to the endpoint's text
index where it declares one and to an anchored label filter where it does not — sent
by the endpoint client over HTTP, paged by `LIMIT`/`OFFSET`, budgeted by timeout and
rows, and written to the workspace like any other table with its receipt graded
`sparql`. Statistics for R1 and the guard come from the graph's descriptor artifacts
(VoID), self-reported; where they are absent the guard treats the pattern as
unbounded and says so.

### C6. Receipts

Per table and per plan (D5), as a JSON document with a schema in `docs/`. A receipt
is written beside its table as a sidecar and into the Parquet footer metadata (D15),
attached to the DuckDB view as a comment, and returned from every `fetch`. It records
the graph, release, content digest and origin; the plan; every request with its
`KGF-Request-Id`, bytes and completeness; every approximation; and, for each input
the step consumed, whether it was *derived* from an earlier step's output or
*asserted* by the agent or the user (C9). A derived table — the result of a `sql`
statement — carries the statement and the receipts of the tables it read, so
derivation is one hop per table. Every receipt carries a source grade (D17).
*Landed 2026-09-09* as `kgfq.workspace.receipts` with `docs/receipt.schema.json`;
the receipt is embedded in the Parquet footer under `kgfq.receipt` and written
beside the file. A fetched table always has the positional columns
`s, p, o, o_kind, o_lang, o_datatype` (plus `binding` when bindings were supplied), even
when the caller named its variables: every fetched table is a set of triples, which
is what makes `cite` a lookup. The content hash is over canonically sorted rows, so
replay compares equal results that arrived in another order. A
`sparql` receipt records the endpoint, the query text verbatim, the time, the version
signal if the endpoint offers one (VoID `pav:version` or a modification date, else
the registry's `retrieved_at`), and the result hash; an `external` receipt records
the import step's description, the content hash, and the URL, time and `ETag` when
the library fetched the data itself.
*Landed 2026-09-10*, kind `sparql`, schema `kgfq.receipt/2` (`kgfq.sparql.receipt`).
Decided while building: a `sparql` receipt wraps `kgf-sparql`'s receipt rather than
copying it. The engine's request list runs to about 500 bytes per request, half of it
URL — 2.5 MB at a 5,000-request abort, 10 MB at the 20,000 budget, and 43 of the 94
corpus receipts exceed 500 requests — and it is the evidence the 2026-09-10 planner
fixes were read from, so it is kept whole, filed beside the table as
`tables/<name>.kgf-sparql.receipt.json` and pointed at by path and sha256. The kgfq
receipt, which is also the Parquet footer and the log record's summary, carries the
sources (each graded), the query as run, the lint and normalization record, the
engine's outcome, budget and versions, the totals, and `requests_by_shape`: the list
folded by method, route, bound positions and predicate, which is the view the fixes
were made from (the worst corpus receipt folds to 14 shapes). A step that produced
no table — a run aborted at a budget, an engine error, an ASK — still writes its
sidecar, since an abort at a named budget is a finding (D2); a stale table of the
same name is removed so a name never describes a table its receipt does not. SELECT
results are stored with four columns per variable (`v, v_kind, v_lang, v_datatype`);
CONSTRUCT and DESCRIBE results take a fetched table's columns.

*Changed 2026-09-24:* a term's kind is a column, `o_kind` (and `v_kind`): `iri`,
`literal` or `bnode`. Until now a plain literal carried `xsd:string` spelled out so that
"no language and no datatype" could mean IRI: the datatype column doubled as a type tag,
an IRI and the string of its text differed only by that spelled-out datatype, and the
rule was lopsided — a tagged literal stored no `rdf:langString`. Now the kind is stated,
and a literal's datatype is stored only when it says something: a plain literal has
neither language nor datatype, however the server spelled it (RDF 1.1: one term, one
encoding). `term_columns` is the one encoder; `sparql.results` converts rdflib terms to
it rather than keeping a second copy of the rule. A blank node keeps its `_:` in the
value, so `s`, which has no kind column, stays unambiguous. `cite` and `join` read the
kind rather than infer it, and a browser client, `../kgf-explorer`, stores its tables the
same way. Tables written before this have no `o_kind`
and hash differently.

*Decided 2026-09-24 — what `complete` means.* Schema `kgfq.receipt/3`. A table is
incomplete only when its read left rows the server still had — it stopped at a response
with rows remaining (`max_rows`: the rest of the page, a cursor not followed, a bindings
chunk not sent), or a response said `complete: false` and was not a page limit or budget
interruption whose cursor was followed. It had been the conjunction of every response's
flag in the step, which receipted every paged read incomplete: kwg-places
`?z <…/ontology/hasGEOID> ?zip` on `apps.okn.us/kgf`, 33,791 rows (the exact count) over
four pages of 10,000, came back `complete: false`; statistics paging `/schema` in a
fresh workspace did the same to any fetch; and one complete page cut short by
`--max-rows` read complete. The facts are recorded where they are known. The endpoint's
`Paged` read marks a response `continued` when it follows the cursor and `stopped` when
its caller stops with rows remaining, which a generator cannot say once its caller
breaks off. The receipt reads only the responses that carried rows: the plan's `/count`
and the statistics' pages are the plan's evidence. `truncation_reasons` stays the trace,
every reason those responses gave; `incomplete_because` says what the table is missing
for. A `sparql` receipt's completeness was already the engine's contract, and it now
names the budget that ended the run, or `error`. A followed cursor counts as continued
without loss after `page_limit`, `time_budget` or `response_bytes` (`CONTINUABLE`). The
first draft admitted `page_limit` alone, the only reason among the corpus's 9,671
truncated engine responses (27 of its 58 complete runs have one). *Widened 2026-09-25*
on doc 03 §3.6's promise that any cursor resumes with no loss and no duplication. kgf-rs
`92af565` backs it: `TruncationReason::resumes()` allows a cursor on those three and
`candidate_budget`, never on `cell_overflow` or `partial_failure`. Its test
`a_response_stops_at_the_byte_budget_and_resumes_from_where_it_stopped` pages a fragment
on a 200-byte budget back to the unrestricted fragment. `time_budget` is declared there
but not yet emitted. `candidate_budget` is not admitted. A budget stop without a cursor,
which kgf-rs also makes, stays incomplete, since nothing was followed. The `--max-rows`
approximation is written only when rows were left, so a cap equal to the count no longer
contradicts `complete: true`. Tested in `tests/test_fetch.py` at five-row pages and in
three bindings chunks that span pages: complete when read to the end; stopped mid-page,
at a page boundary, mid the last page and after an earlier chunk, incomplete; stopped at
the last row, complete. The widened set is tested over records: `kgf serve` takes no
budget, and the fixtures fit its 64 MB and 2 s.

*Planned 2026-09-24 — the unmatched report.* A bound fetch (`fetch --values`,
`fetch(ds, pattern, bindings)`, `hydrate`) records in its receipt a `bindings` block:
`sent`, `matched`, `unmatched`, and `unmatched_sample` (the first eight, in request
syntax), counted over the distinct input tuples, since a repeated value answers once and
the local-filter route keeps only its first index. It is read off the table's `binding`
column — an input no row answers found nothing — so it is the same whichever route R2
chose, pushed bindings or a whole fetch filtered here. It is not an approximation: the
table is exact for the values that matched. It is the number a reader needs before
trusting the next join, and the receipt is where a silent loss becomes a finding. Found
in `../kgf-explorer`: ruralkg writes 899 ZIPs without their leading zeros, space-padded
(`"1002 "` for 01002), so binding its postal codes to kwg-places' `hasGEOID` matched
4,887 of 5,420, and the 533 lost ones, which dropped New England, New Jersey and Puerto
Rico from an answer, showed only once a sample was printed; after repair, the 64 still
unmatched were well-formed USPS ZIPs with no Census ZIP area, a different loss the same
report caught. Schema `kgfq.receipt/3`. The handle `fetch` prints and returns (§6, §7)
carries `unmatched` and the sample, not only the sidecar: that is where it changes what
an agent does next. Waits for the paged-completeness fix to `fetch_receipt` to land.

### C7. Resolve and labels

Fan-out `/search` across the chosen datasets, merged by `match_kind` class and rank,
never by raw score, because BM25 scores are not comparable across independently built
indexes (doc 03 §3.4.5). `/labels` in batches of `max_label_iris`; label hydration
of tables as a separate, explicit step, since `labels=true` on row operations is not
implemented and the label-role declarations are wrong on a third of the federation
(results §7.4a) — hydration against a wrong declaration returns nulls faster.

### C8. The sandboxed DuckDB

D6 as code: one module constructs the connection, applies the settings, owns the
scratch directory, and exposes the allowlisted statement kinds. Nothing else in the
codebase opens a DuckDB. The connection is in-memory with a view per workspace table
and a scratch file for spills (D15); the workspace's Parquet files are the store, and
a `CREATE TABLE … AS` result leaves DuckDB only by being exported to `tables/` with
its own receipt (C6). Read-only views over files also mean two `kgfq` processes never
contend for a writer lock.

### C9. The workspace and the log

The directory an analysis lives in (D15): the current directory, or `--workspace`.

```
<workspace>/
  .kgfq/               session state — endpoints, pins, the statistics cache, the
                       DuckDB scratch file; disposable, since every log record
                       carries its own pins
  kgfq.log.jsonl       the session log: one record per library operation, append-only
  tables/
    genes.parquet      s, p, o, o_kind, o_lang, o_datatype — or a derived table's columns
    genes.receipt.json the receipt, also embedded in the Parquet footer
  report.md            written by `kgfq report` (C11)
```

A log record, with a schema in `docs/` beside the plan's and the receipt's: step id,
time, skin (`cli` or `mcp`), the command and its arguments, the pins in force, the
plan, the receipt, the output table and its content hash, and each input classified
as **derived** — found among the outputs of earlier steps (resolve results, fetched
tables, files the session wrote), with the producing step — or **asserted**, typed
by the agent or the user. That classification is set membership over logged outputs,
costs nothing, and needs no cooperation from the model — the property `mcp-okn`'s
record lacked when it asked the model to flag exploratory queries and not to paste
looked-up constants (doc 12 §12.5.4). The library writes the log; both skins inherit
it; the MCP server's workspace is its working directory or `KGFQ_WORKSPACE`.

`kgfq import <file | url> --from "<description>"` is the one door for data from
anywhere else (D17): another MCP server's result the agent saved to a file, a CSV
the user had, an API. It writes a table with a receipt graded `external`, carrying
the description as the caller's claim, the content hash as the library's fact, and
the URL, time and `ETag` when the library fetched the data itself. A `--values` file
that is not a workspace table is imported the same way, implicitly, so that no value
reaches a bindings request without a step behind it.

*Verified by:* a session re-run from its log alone in an empty directory produces
byte-identical tables (C10); a pasted constant is classified asserted; the same IRI
copied from a `resolve` result is classified derived, naming that step.

*Landed 2026-09-09* as `kgfq.workspace.store` and `kgfq.workspace.log` with
`docs/log-record.schema.json` (`tests/test_workspace.py`, `tests/test_cli.py`).
Derived-versus-asserted is membership of a value in the `s`/`o` columns of any
workspace table, read through Arrow; a `--values` file that is not a workspace
table is classified `external` with its SHA-256. The `resolve` results side of the
classification waits on C7. `kgfq import` is not written yet.

### C10. Replay

`kgfq replay` re-executes the log's **stored plans**, not its intents, against the
pinned releases, and compares each output hash. Re-planning from intent is a separate
mode for the harness (§8), so a change to the strategy function never makes a user's
evidence fail to verify. The properties D15 lists make this exact: results are hashed
after a canonical sort, the DuckDB version is recorded, `/sample` seeds are recorded,
and a budget abort replays as the same abort. When a provider has withdrawn a
release, replay reports the source unavailable and stops; the workspace's tables and
receipts remain the evidence of what was fetched, and the report (C11) keeps
"verified against live sources on <date>" apart from "archived at fetch time". A
`sparql` step is re-executed and compared, and a mismatch is reported as *source
changed since fetch*, not as an error; an `external` step replays from the archive,
since the library cannot re-fetch what it did not fetch.

*Verified by:* every §8 corpus run replayed to identical hashes; a release removed
from a fixture server producing the unavailable status rather than a different
answer.

### C11. Cite and report

`kgfq cite <table> --row N` finds, for each IRI and literal in the row, the triples
in the workspace's fetched tables that carry the value, grouped by graph and release
with the request id that fetched them — "these specific triples from this specific
graph" as a lookup. It is evidence retrieval, not lineage: for an aggregate, the
citation is the statement and the receipts of its inputs. Column-level lineage
through arbitrary SQL is not promised (§12).

`kgfq report` writes the provenance document from the log and nothing else: the
sources used, as a bibliography grouped by grade (D17) — bundles with release,
digest, origin, requests and bytes, SPARQL endpoints with their version signal and
query count, imports with their descriptions; every step with its plan, completeness
and grade; every approximation; the asserted constants, first; a grade beside every
cited number with the step at which its weakest input entered; and the replay
status. It is the successor to `mcp-okn`'s `create_reproducibility_record`, with the
difference that nothing in it is typed by the model.

The narrative is the boundary, and it is drawn on purpose. The skill teaches the
agent to cite tables and rows in its final answer with a short reference syntax
(`[t:genes#12]`, `[step:7]`); `report` resolves each reference to a log step and
flags a number in prose with no reference behind it. Whether a conclusion follows
from the cited rows is a judgement — a second agent can check it and §8 can score
it — and the tool claims only what it proves: every number came from these triples
through this script.

*Verified by:* the trust corpus (§8).

## 5. SPARQL — how `kgfq` does and does not support it

This is the section with the most to get wrong, so it is stated as positions.

### 5.1 The position

SPARQL is supported as a **complete-answering** path delivered by **`../kgf-sparql`**
— stock Comunica plus the KGF actor suite — which grows automatic statistics supply,
a budget, and a receipt as features of its own command line (§5.2). `kgfq sparql` is
a wrapper: it runs `kgfq`'s linter, launches `kgf-sparql` when it is installed, and
files the receipt like any other. The path exists for three audiences: **people** who
already have SPARQL, **existing corpora** (the 89 registry queries are its acceptance
suite), and **the evaluation harness**, where it is Lane A. The strategy function does
not use it (§5.5).

It is **not** the agent's default, **not** an MCP tool, and **not** a promise that
arbitrary SPARQL over fragments is fast. Each of those is a measured position, not a
preference.

### 5.2 What "via kgf-sparql" means, concretely

Three features `kgf-sparql` needs regardless of `kgfq`, because a bare
`kgf-sparql` invocation today behaves exactly like stock Comunica unless the caller
hands it statistics by context key (its README). They are this plan's asks of
`../kgf-sparql`, in the order a first release needs them:

1. **Statistics it fetches itself.** Given a KGF `/fragment` URL, follow the
   release's typed links to `/schema` and `/void`, build the VoID registry the
   selectivity actor reads, and cache it per release — releases are immutable. This
   is the one thing the engine most needs from a client and the reason it should
   need no client at all.
2. **A budget.** `--max-requests`, `--timeout`, `--max-rows`, counted by the engine's
   own `fetch`. Comunica has no plan-level budget — the fifteen Lane A failures prove
   it — so an exceeded budget aborts the engine, exits non-zero, and still writes the
   receipt. Never a heap death.
3. **A receipt**, as JSON to `--receipt <file>`: requests, bytes, requests that
   pushed bindings, wall clock, statistics used, pinned releases, and the contract —
   the solution set the query denotes, or aborted at a named budget, nothing in
   between.

Given those, `kgfq sparql query.rq --dataset a --dataset b` does, in order:

1. **Lint** (`kgfq.lint`, §5.6). Diagnostics are printed; nothing is rewritten unless
   `--normalize` asks for the proven-equivalent transformations, and every one applied
   is recorded. The linter is `kgfq`'s — `kgf-sparql` does not carry a second copy.
2. **Select sources.** From `--dataset`, or from the registry's `#+ tags` header
   convention when present; each resolved through C2 to a pinned release and its
   `/fragment` URL — or, for a SPARQL-only source, to its declared `services.sparql`
   endpoint, graded `sparql` in the receipt (D17). No `SERVICE` to endpoints the
   caller did not name (the registry corpus had queries that silently depended on a
   remote OARD endpoint).
3. **Launch** `kgf-sparql` with those sources, the budget, and a receipt path; the
   binary is found through `KGFQ_KGF_SPARQL` or `PATH`, and when it is absent the
   command says how to install it rather than degrading to anything else.
4. **File the receipt** through C6, with the lint diagnostics and any normalization
   attached, so a SPARQL run and an integration plan are audited the same way.

*Landed 2026-09-10* as `kgfq.sparql` (`engine`, `results`, `receipt`, `normalize`)
and `kgfq sparql` (`tests/test_sparql.py`, against a real `kgf-sparql` over the two
fixture servers). The three engine features exist in `../kgf-sparql` (its README, "Use
as a CLI"; commits `633c03c`–`2713fbb`): `kgf@`/`sparql@` sources, `--max-requests`,
`--max-bytes`, `--max-rows`, `--timeout`, a receipt on every exit path, exit 0/1/2/3.
Decided while building: the engine is found through `KGFQ_KGF_SPARQL` (an executable,
or the package's `bin/kgf-sparql.js` run by `node`) or `PATH`, checked before any other
work, and its absence is an install message, never a fallback; sources are
`--dataset` (repeatable) or the file's `#+ tags`, each resolved through C2 to its
pinned release's `/fragment` URL, and `--sparql-endpoint` names an endpoint
explicitly, graded `sparql` — a descriptor-declared `services.sparql` waits for a
server that declares one; the default budget is the corpus's (20,000 requests, 256 MiB,
60 s; `0` for none), overridable per run and recorded; a strong diagnostic or KGFQ008
refuses with exit 2 and a logged step unless `--force`; exit 3 mirrors the engine's
abort, including `--max-rows`, whose rows are written with an approximation like
`fetch`'s R3; an engine error is exit 1 with its message verbatim; the engine's
output file lives in `.kgfq/scratch` only until it is a table. `--normalize` applies
KGFQ002 (the BIND replaced in place) and KGFQ001 (a deduplicated `VALUES` at the top of
the outer group, the FILTER kept as the residual check) only when the filter's own
boolean structure proves the finite set — `=`/`IN` under `&&` and `||`, nothing under
`!` or across variables — and only for IRIs and string literals, since `=` compares
typed literals by value where `VALUES` matches by term; every rewrite is re-parsed and
re-linted, reverted and reported when the diagnostic survives, and the receipt carries
the text as given and as run. One linter change came with it: KGFQ001 no longer fires
for a variable already pinned by `VALUES`, so a residual filter is not a diagnostic.

### 5.3 The actor suite — what lives in `../kgf-sparql`, and what does not

The suite is small and its ceiling is known (guidance §5: "the ceiling is low and
close, and that is a useful thing to know before investing").

| piece | status | what it is for |
|---|---|---|
| `@kgf/actor-rdf-join-selectivity-void` | **exists**, `accuracy 0.8` | replaces the syntactic selectivity heuristic with one computed from published VoID; the tail fix of §1. Works for any TPF/brTPF endpoint that publishes VoID — KGF is simply one that publishes good VoID |
| `@kgf/actor-query-source-kgf` | **to build** — doc 06 §6.4's `QuerySourceKgf`, client-suite §6.1 | a native KGF source beside the stock brTPF one: discover the descriptor and manifest; page at `max_limit` instead of Comunica's unset `limit` (the 100× lever); send bindings through QUERY/POST sized by rows *and* bytes, instead of `values=` in a GET that overflows on long IRIs; report cardinality as `exact` (a fidelity fix, not a performance one — guidance §3a); later, `o.text` pushdown for object-position `CONTAINS`/`REGEX` with the SPARQL filter retained as the residual check. Identified by URL through a registry the client passes in context (guidance §5.2.2), because the client configured the sources and already knows which are KGF |
| engine config | **to change** | `blockSize` raised to the published `max_bindings` for the brTPF path (guidance §5.1). With the native source carrying bindings in a body this becomes the source's concern; the config keeps the stock brTPF path honest for sources the client did not configure |
| stock brTPF | **keep working** | the interop guarantee. Stock Comunica queried KGF with no KGF-specific code, single and multi-source, in the benchmark. Nothing in the suite may break that |
| the CLI features of §5.2 | **to build** | statistics fetched per release, a budget, a receipt — the three things that make `kgf-sparql` a complete tool rather than a library a client has to feed |
| upstream contributions | **track in kgf-sparql**, guidance §9 | source-declared bind batch size; the VoID `distinctSubjects`/`distinctObjects` transposition (merged); the data-driven selectivity pattern; a round-trip-aware cost model for paged sources |

**What is deliberately not an actor.** The guard and the budget are not Comunica
components: the engine has no notion of a plan budget, and putting one inside an
actor would mean re-deriving the request count from inside the planner's own
estimates. The linter is not an actor either — it runs before the engine and talks to
the author. And there is no "pattern-count actor": the six-pattern cliff is enforced
by `kgfq sparql` refusing above the review threshold unless `--force`, with the
diagnostics that say how to decompose.

**Where the code lives.** `../kgf-sparql`, as a workspace over *published*
`@comunica/*` releases — never a fork (guidance §6.9; the engine has seven direct
dependencies, not 261). `kgfq` depends on it only as an executable, and records the
version it launched in the receipt.

**Speculative, parked in `kgf-sparql`'s backlog.** A join actor that materializes
both sides into DuckDB and streams the result back — Comunica plans, DuckDB
executes — would make heavy *complete* SPARQL survive its intermediates. Feasible,
and never a reason for `kgfq`'s language: DuckDB's out-of-core execution is engine-
level and available from every language, `kgf-sparql` is Node regardless, and the
workload it would rescue is the one §5.7 routes to tier 2. Worth trying only after the
native source exists and the registry corpus shows how much of the tail is heap rather
than plan.

### 5.4 What `kgfq` must not do with SPARQL

- **No `sparql` tool in `kgfq-mcp`.** Doc 12 §12.5.3 measured a passthrough tool
  carrying 86% of an agent's traffic, and Lane A showed that handing the same SPARQL
  to a competent engine does not rescue it. If an agent needs SPARQL it gets `kgfq
  sparql`, or `kgf-sparql` itself, from a shell, where the linter, the budget, and
  the receipt stand between it and the network. *Open 2026-09-10:* the evidence for
  this exclusion predates the budget, receipt and linter; §11 item 11 proposes a
  bounded `sparql` tool and the evaluation that would decide it.
- **No approximation inside the SPARQL path.** No `LIMIT` pushed through a join it
  cannot reason about, no preferred-label substitution for a multi-valued
  `rdfs:label`, no sampling, no "helpful" `DISTINCT`. Those belong to the integration
  contract with a receipt (D2). The two contracts can return different answers to the
  same question and the difference is invisible in the result; the discipline that
  keeps them apart is that `kgfq sparql` never makes those moves.
- **No silent rewriting.** `--normalize` applies only proven-equivalent
  transformations — a finite equality disjunction on a definitely bound variable to a
  deduplicated `VALUES`, a constant `BIND` used as a selection to a `VALUES` — and
  lists each in the receipt. Disconnected BGPs, `OPTIONAL` bridges, and aggregation
  shapes get a diagnostic and the author's decision, never a guess about intent
  (client-suite §6.2).
- **No SPARQL engine of our own, and no client-side "restricted profile".** Doc 06
  §6.5's profile is a server feature, phase 2 or later. The client's job is to lint,
  budget, and decompose.
- **Not the executor for heavy plans.** A streaming engine is right for a browser
  over linked data and wrong for a 500 MB intermediate (implications §3.8). Heavy
  work materializes into DuckDB (D4); the engine is for people's SPARQL, not for the
  strategy function's plans.
- **Not judged by request totals alone.** Bytes, client memory, completeness, and
  answer equivalence count (client-suite §9). A plan that halves requests by moving
  63 MB instead of 31 MB is the right trade on a local-first client and must be
  reported as a trade.

**And one thing that looks like an exception and is not.** For a graph whose only
service is SPARQL, the strategy function compiles the intent to a fixed template
(D16, C5). The SPARQL is code's; the agent neither sees nor writes it; the receipt
records the query text verbatim and grades the table `sparql` (D17). The rule this
section enforces is against *model-authored* SPARQL, and a template is not that.

### 5.5 Small plans are the library's own — the contracts meet nowhere in code

The first draft had the strategy function compile a small conjunctive intent to
SPARQL and run it through the engine, on the ground that 72% of the benchmark
corpus is in the ≤ 6 pattern band where stock Comunica finished 36 of 38. In Python
that convenience is not available, and it turns out not to be needed: a small
conjunctive plan is a bind-join loop — order by R1, extend each step with a bindings
`/fragment` chunked by C1, stop at the guard — about twenty lines in `kgfq.plan`,
executed by C5's streaming path with rows in memory. It also keeps a property the
in-process design could not: the library has no execution path whose cost it cannot
account for step by step, and no dependency on the engine at all. The only contact
between the two contracts is the process boundary in `kgfq sparql`.

### 5.6 The linter

Eleven rules from the client-suite report, already prefixed `KGFQ`:

| rule | severity | diagnostic |
|---|---|---|
| `KGFQ001` | warning | finite equality / `IN` filter could be `VALUES` |
| `KGFQ002` | warning | constant `BIND` used as a remote selection constraint |
| `KGFQ003` | warning | variable predicate restricted only by namespace or string filtering |
| `KGFQ004` | strong | required BGP has disconnected components — a Cartesian product |
| `KGFQ005` | strong | an `OPTIONAL` is the only apparent bridge between required components |
| `KGFQ006` | warning | broad label predicate followed by an unanchored text filter |
| `KGFQ007` | info | `LIMIT` follows a blocking `DISTINCT`, `GROUP BY`, or `ORDER BY` |
| `KGFQ008` | info | query exceeds the complex-BGP review threshold (default six patterns) |
| `KGFQ009` | warning | aggregation follows multiple multi-valued joins or optionals |
| `KGFQ010` | warning | unbounded property path lacks a selective endpoint |
| `KGFQ011` | info | `SELECT *` projects variables the caller does not need |

Plus one this repository adds: **`KGFQ012`, warning — transitive path over a predicate
the dataset descriptor declares already closed** (§4 C4). The author guide in `docs/`
is the same eleven rules in prose, written for a coding agent, following client-suite
§3.

The linter is a module with its own entry point — `kgfq lint`, `uvx kgfq lint` — so
registry CI can run it over the 89 committed queries and refuse a new one that denotes
a Cartesian product, the cheapest place to catch what cost a timeout in the corpus
run. Algebra comes from `rdflib`'s SPARQL parser, which is slow but not on any hot
path; if the 468-query replay makes that hurt, the fallback is `spargebra` behind a
small Rust extension — the benchmark's own shape analysis already used it.

*Landed 2026-09-09* as `kgfq.lint` (`analysis`, `rules`, `header`; `tests/test_lint.py`;
the author guide is `docs/sparql-author-guide.md`). rdflib's algebra is walked once
into a `QueryShape` — required and optional patterns, unions, subselects, SERVICE
blocks, filters classified by what they constrain, constant binds, VALUES, paths,
modifiers — and the twelve rules read that. Decided while building: connectivity
(KGFQ004/005) is union-find over variables, where a FILTER over two variables and a
non-constant BIND count as edges, a UNION is one node bound by all its branches,
and a disconnected component whose every pattern has a constant subject (one
entity's values beside the main pattern) is a *warning* rather than a refusal, since
the product is one row wide; KGFQ006 fires only when the label pattern's subject is
not pinned by a constant, VALUES or a constant BIND, so a residual filter after
`resolve` is never flagged; KGFQ012 takes its closed predicates from `--closed` or
from the dataset descriptor when it ever declares them, since doc 03 has no field
yet; a query that does not parse is exit code 2, never a crash. Over the registry
corpus (97 queries now, 89 when the plan was written): 96 parse, one has an
undeclared prefix in the corpus itself, 25 are clean, every gate in §8 fires —
LINCS is the only strong refusal, with both KGFQ004 and KGFQ005 — and the totals
(32 × KGFQ007, 32 × KGFQ008, 16 × KGFQ009, 8 × KGFQ001, 7 × KGFQ002, 7 × KGFQ011)
are the corpus saying where its cost is. `--normalize` is not written; it belongs
with `kgfq sparql` — *landed there 2026-09-10*, see §5.2.

### 5.7 The ceiling, stated so nobody sells past it

The actor suite rescues the pathological tail and leaves the median alone. Broad
analytic SPARQL over fragments — `DISTINCT` over 1.68 M triples to find 75 values,
grouped counts over whole relations — stays slow or infeasible through any fragment
client, because the client must read the input (client-suite §5.1). Two responses,
both outside this repository: a bounded **facets** operation on the server (§10), and
doc 08 §8.4's **tier 2** — a SPARQL endpoint declared beside the bundle in the
service descriptor's `services` block. `kgf-sparql` should be able to *route* to a
declared tier-2 endpoint on request, never silently, with the receipt saying which
substrate answered (question 2, §11). Until then it stops at its budget and says
why.

## 6. `kgfq` — the CLI

The intent surface as shell commands, which is what gives a bash-native coding agent
full access with no MCP setup. Commands follow doc 06 §6.2.2's bootstrap ladder:

| command | rung | backed by |
|---|---|---|
| `kgfq endpoints add\|list\|remove <url>` | find | the workspace's graph endpoints (D14, C2) |
| `kgfq datasets [--iri X \| --class C \| --text Q]` | find | registry + service descriptors (C2) |
| `kgfq summary <ds>` | orient | `/summary`, Markdown to the terminal |
| `kgfq schema <ds> [--class C \| --predicate P \| --projection …]` | drill | `/schema` |
| `kgfq count <ds> <pattern> [--values file]` | verify | `/count`, batch form |
| `kgfq sample <ds> <pattern> [-n]` | verify | `/sample` |
| `kgfq resolve <text> [--datasets …] [--role label]` | situate | fan-out `/search` (C7) |
| `kgfq fetch <ds> <pattern> [--values file] --out t.parquet\|.csv\|.json` | act | the strategy function over `/fragment` |
| `kgfq hydrate <ds> --subjects file --properties p1,p2 --out …` | act | bindings per property today, `/star` when routed |
| `kgfq labels <ds> --iris file` | act | `/labels` |
| `kgfq import <file\|url> --from "<description>"` | act | a table graded `external` — the one door for other data (D17, C9) |
| `kgfq join <plan.json>` | act | a multi-step plan through the executors, into DuckDB |
| `kgfq sql "<select>"` | local | the sandboxed DuckDB over fetched tables |
| `kgfq explain <intent>` | debug | prints the plan (D12) without executing |
| `kgfq lint <file.rq>` | author | `kgfq.lint`, §5.6 |
| `kgfq sparql <file.rq> --dataset … [--endpoint url]` | complete | lint, then launch `kgf-sparql` (§5.2); refuses politely when it is not installed |
| `kgfq raw <method> <path> [--body]` | debug | one unplanned request, output marked `unplanned` |
| `kgfq receipt <table>` | audit | the receipt attached to a table |
| `kgfq replay [--from N]` | audit | re-execute the log's stored plans and compare hashes (C10) |
| `kgfq cite <table> --row N` | audit | the source triples behind a row (C11) |
| `kgfq report` | audit | the provenance document, generated from the log (C11) |

*Landed 2026-09-09* (`kgfq.cli`, `tests/test_cli.py`): `endpoints`, `datasets`,
`summary`, `schema`, `count`, `sample`, `explain` with R1 and R2, `fetch` to a
workspace table or a `.parquet`/`.csv`/`.json` path, `receipt`, `log`, `pins`. A
pattern is one argument of three terms in doc 03 §3.3 syntax, `?` for an anonymous
variable and `?name` for a named one; `--values` is JSON or a tab-separated file
with a `?var` header, each of whose variables the pattern must name (C1, 2026-11-06);
`--max-rows` records an R3 approximation in the receipt;
`--endpoint` overrides the workspace's list for one invocation.
*Landed 2026-09-10*: `sparql` (§5.2) — `kgfq sparql <file.rq> [--dataset …]
[--sparql-endpoint …] [--out name] [--max-requests N] [--max-bytes B] [--max-rows R]
[--timeout S] [--normalize] [--force]`; the handle names the contract, the table, the
budget that ended an aborted run, the lint codes and the engine receipt's path.

Conventions: JSON on stdout when not a TTY, tables when it is; `--out` writes files;
the working tree is the workspace (C9) — `.kgfq/` holds the session pins, the
release-scoped statistics cache and the DuckDB scratch file, `tables/` the evidence,
and `kgfq.log.jsonl` the script — so a multi-step task never straddles a release, a
second invocation reuses what the first learned, and parallel invocations write
files rather than contend for one database. Every error is the server's
problem document, verbatim — the benchmark called those messages excellent for an LLM
client, and the CLI must not paraphrase them.

## 7. `kgfq-mcp` — the MCP server

A thin skin. Doc 06 §6.2.1's tool table, minus what the server does not yet serve:

| tool | backed by |
|---|---|
| `find_datasets` | C2 |
| `dataset_summary` | `/summary` — the card straight into context |
| `dataset_schema` | `/schema` |
| `resolve` | C7 |
| `label` | `/labels` |
| `count`, `sample` | `/count` incl. bindings, `/sample` |
| `fetch(ds, pattern[, bindings]) → table` | the strategy function; returns a **handle and a receipt, never rows**; a bound fetch's handle says how many values matched nothing, with a sample (C6) |
| `hydrate(ds, subjects, properties) → table` | bindings today, `/star` later |
| `sql(query)` | the sandboxed DuckDB (C8), allowlisted statements only |
| `cite(table, row)`, `report()` | C11 — so the agent can cite while it writes |
| `import(path \| url, description) → table` | C9 — the one door for other data (D17) |

Cards and manifests are exposed as MCP **resources** as well as tool results. The
official Python `mcp` SDK, stdio only (D13): the DuckDB runs on the user's machine,
which is the cost-shifting thesis in physical form, and there is no hosted variant.
The server writes the same log through the same library as the CLI (C9). There is no
`sparql` tool (§5.4).

Continuity with `mcp-okn`: `list_kgs` → `find_datasets`, `describe_kg` →
`dataset_summary`, `get_schema` → `dataset_schema`; `probe_namespaces` and
`find_crosswalks` become `resolve`, `sharedKeys` (R6) and, once routed, the namespaces
inventory; `sparql_query` has no successor here by design. Same language, so what is worth
keeping from `mcp-okn` — tool shapes, the benchmark's `agents.py` driver, the
analysis half of its skill — moves rather than being rewritten. The agent skill in `skills/`
teaches the loop — discover, resolve, probe with counts and samples, choose the fetch
strategy, integrate in SQL, verify cardinalities — with two disciplines: count before
fetching, and report the denominator, meaning how many of what an analysis started from
survived each fetch and join, with a look at a sample of what did not. C6's unmatched
report makes the fetch half mechanical; a SQL join that drops rows stays a habit the
skill has to teach. It inherits from `mcp-okn`'s `okn-bioanalysis` skill what is about
analysis rather than about SPARQL.

## 8. Evaluation — a deliverable

Three corpora, pinned by commit, with frozen baselines per bundle release
(client-suite §7.1):

| corpus | size | what it tests |
|---|---|---|
| `mcp-okn/benchmark/dataset.jsonl` at `4e49e2c` | 80 records, 68 runnable; `smoke.py` supplies reference rows | single-graph correctness and cost; its `score.py` is execution-match, column-order independent |
| `okn-registry/docs/registry/queries` | 89 `.rq` with `#+ tags` | the acceptance suite for `kgfq sparql`; `kgf-sparql-bench/test/registry-corpus.py` is the existing runner to port |
| `mcp-okn/docs/examples/*` reproducibility records | 468 queries, 14 studies, 39% cross-graph | replay for cross-graph behavior; one study (`Instrument-Criticality`, two graphs, ~38 queries, a second run under another model already checked in) re-run end to end as the doc 06 §6.1 ablation |

Arms, run back to back against the same releases: stock Comunica over brTPF;
`kgf-sparql` with the actor suite; `kgfq sparql` with lint and budget; `kgfq`
integration plans — the eight hand-written Lane B plans first, as the function's own
regression tests, then agent-driven; QLever for ground truth, with data skew reported
per task since the bundles and the consolidated store have drifted.

Metrics per task: rows and a deterministic solution hash; requests, binding-bearing
requests, bytes; wall clock and peak heap; the plan; contract used; completeness and
every approximation. Gates by failure class rather than corpus totals (client-suite
§7.2): finite filter → `VALUES` (ProKN diseases), class-star (SOCKG samples),
selective `OPTIONAL` (FIO, hydrology), predicate inventory (NCIPID), text candidates
(ProKN Alzheimer's), blocking `DISTINCT` (SCALES labels), broad aggregation (NASA
keywords), and the disconnected query (ProKN LINCS), which must produce `KGFQ004`.

A separate federation corpus (client-suite §7.3) before any multi-endpoint claim:
two- and three-endpoint joins, lopsided pairs, disjoint identifier spaces, partial
capability availability. The least-privilege scenarios of D6 — literals carrying
SQL syntax or instruction-like text, against the MCP path — run here too.

A trust corpus beside it, for C9–C11 and D17: a session with a pasted constant,
which the report must list as asserted; the same session with the constant taken
from a `resolve` result, which it must list as derived; a label containing SQL
syntax, which must never reach a SQL identifier; a `LIMIT` pushdown (R3) whose
approximation must appear in the report; a number in the agent's answer with no
citation, which the report must flag; a SPARQL-only fixture whose data changes
between fetch and replay, which must report *source changed* rather than fail; an
imported file whose description must appear in the bibliography and whose rows must
grade every table derived from them `external`; a value typed from an external API
into a bindings argument, which must show as asserted; and every run in every corpus
replayed to identical hashes (C10).

Two method notes carried over from the run: for the `kgf-sparql` arms, write results
to files, never pipes (Node truncates unflushed writes at exit and pipes cap at
64 KB); and count requests at the server's access log — `kgf-rs` unit 22, one record
per response, joined on `KGF-Request-Id` — never through a proxy.

## 9. Sequence

**A — the floor (weeks 1–2).** *Started 2026-09-09: scaffold, C1, C2, C3, C6, C9, and
the §6 commands through `fetch` and `explain` are in, 87 tests against two spawned
servers built with `kgf build` plus an in-test gateway proxy, and the CLI verified
against `apps.okn.us/kgf`, and the linter with its author guide; remaining in A: the `kgf-sparql` features and
`kgfq sparql`.* uv project scaffold, CI against a spawned `kgf serve`.
C1–C3, and C9's workspace and log from the first command — C10 and C11 read it, and
a log retrofitted later is how `mcp-okn` came to ask the model to keep one. `kgfq
endpoints`, `datasets`, `summary`, `schema`, `count`, `sample`, `fetch` to Parquet and
CSV, `explain` printing a plan that has only R1 and R2. `kgfq.lint` with the twelve
rules and the author guide. In `../kgf-sparql`: the three CLI features of §5.2 —
statistics fetched per release, `--max-requests`/`--timeout`, a receipt — and
`kgfq sparql` launching it. The receipt and log-record schemas carry the source
grade from their first version (D17).
*Exit:* the registry corpus runs through `kgfq sparql` with the same 45 non-empty
results as the kgf-sparql run, every timeout is a budget abort with a receipt, and
every command has written its log record.
*Measured 2026-09-10* (`bench/registry_corpus.py`; `bench/registry-corpus-20260910.json`;
budget 5,000 requests, 256 MiB, 30 s, to match `../kgf-sparql-bench/test/registry-query-results-cli.json`):
94 of 97 queries run (3 Wikidata skips); 58 complete and 51 non-empty — the same 51 as
the kgf-sparql run, with the same row counts, and 38 of the 39 determinate bags
identical (the exception, `nde/dataset-count-by-agent`, is an unordered `GROUP_CONCAT`
that differs between kgf-sparql's own runs); 30 aborted at a budget, the same 30
(20 `max_requests`, 6 `max_bytes`, 4 `timeout`), each with a receipt; 5 engine errors
verbatim (three the `asDays` extension function, two remote `SERVICE` hosts); one
parse error; 94 log records for 94 runs. The "45" was August's brTPF count; the run the
criterion names now yields 51 after the engine's bind-join fix, so phase A's exit is
read as *the same non-empty results as the kgf-sparql run of the same engine*, which
holds. That answers the first question in kgf-sparql's handoff of 2026-09-10; the
second, default budgets, is answered by `kgfq.sparql.Budget` — 20,000 requests, 256 MiB,
60 s, the corpus's — with the caveat that handoff states: the six queries still
pathological through the native source spend the whole budget until its planner price
(its §4.1) is fixed. Phase A is closed.

**B — the planner (weeks 3–5).** C4 in full, its intent document settled first; C5
with DuckDB, C6, C7, C8, C10. `hydrate`,
`labels`, `join`, `sql`, `resolve`, `import`. The Lane B plans as regression tests. In
`kgf-sparql`: `@kgf/actor-query-source-kgf` with pages at `max_limit` and body
bindings; the `blockSize` config. *Exit:* the eight Lane B tasks reproduced by the
function within 2× of the hand plans' request counts; all fifteen Lane A failures
refused or materialized rather than timing out or dying; every Lane B run replays to
identical hashes.

**C — agents (weeks 6–8).** `kgfq-mcp`, the skill, C11, R6 with `sharedKeys` on the
bindings-`/count` fallback, the SPARQL template executor (D16) against a fixture
endpoint — an `rdflib` graph behind a small HTTP handler, `rdflib` being a dependency
already — the federation and trust corpora, the case-study replay. *Exit:* the
`Instrument-Criticality` study re-run end to end through the MCP server, with
findings, requests and tokens compared against its two checked-in runs; a join
between a bundle graph and the SPARQL-only fixture carrying grade `sparql` through
to the report.

**D — following the server.** Parquet transport when it ships; `/star` behind
`hydrate`; `QUERY /terms` behind `sharedKeys`; the namespaces route behind R6's
precondition; tier-2 routing in `kgf-sparql` once its receipt and doc 03 wording
are agreed. Each is a change in one module because A–C isolated it.

## 10. What this repository needs from the server

In the order the client blocks on them; most are already filed in
`../kgf/docs/benchmark-implications.md` §6 or the two `../kgf-rs/notes` handoffs.

1. **Truthful capability declarations** — `star`, `terms`, `export` declared and
   404ing. Until fixed, D7 probes.
2. ~~**`KGF-Request-Id` and the access record**~~ — **landed** as `kgf-rs` unit 22
   (`c10b325`): every response carries `KGF-Request-Id`, and the server writes one
   shape-tier record per response, with `--log-raw` for the raw tier. The client's
   half — recording the ids in receipts (C6) and sending D11's `User-Agent` (C1) —
   is this repository's, and the census this client's traffic is for can start on
   the first day it serves.
3. **`--public-base`** (`public-base.md`) — or every link the client follows on FRINK
   is wrong. *The server side is deployed (links on `apps.okn.us/kgf` carry the
   prefix) and the client resolves them correctly as of 2026-09-09 (C1).*
4. **Parquet output** — D8 is written so this is a one-module change here.
5. **`/star`** — five benchmark tasks are star-shaped; the bindings fallback costs
   13 requests where one would do.
6. **The namespaces route** — R6's precondition, built and unreachable.
7. **`labels=true` on row operations** — after the label-role declarations are
   corrected, not before.
8. **`QUERY /terms` with a key list**, and the hdtc key→id index behind it — the
   cross-graph fast path.
9. **The "already closed" predicate declaration** in the dataset descriptor
   (doc 14 §14.4) — one field that removes the entire property-path burden of the
   research corpus.
10. **A bounded facets operation** — pattern-scoped distinct values with counts
    (client-suite §5.1). The one new server operation the client work argues for,
    and the only answer to the blocking-`DISTINCT` class short of tier 2.
11. ~~Publisher prose delimited as untrusted in the summary card~~ — **withdrawn**
    the same day: the card's bare description is deliberate, the prose is trusted
    (D6). Two small findings from the same probe are filed as kgf-rs #15 and #16:
    `Allow` on a successful QUERY omits QUERY where the 405 lists it, and the serve
    log carries ANSI codes when stdout is not a terminal. Neither blocks anything.

## 11. Questions for `../kgf`

To be raised against the design docs rather than settled here.

1. **Doc 06 §6.2 promises a Python and a TypeScript library.** This plan ships
   Python (D1) and serves TypeScript users through `kgf-sparql` and the API's own
   self-description rather than a TypeScript client library. Either the doc changes
   or a TypeScript port is scheduled after C.
2. **Tier-2 routing from a client.** Doc 08 §8.4 declares tier-2 endpoints in the
   service descriptor; nothing says whether a client may route a heavy SPARQL query
   to one, or how the answer is labelled. This plan says: on explicit request only,
   named in the receipt (§5.7).
3. **The plan, receipt, intent and log-record schemas** (D5, D12, C4, C9). Doc 06 §6.2.1 sketches a receipt's
   fields; if `kgfq`, the MCP server, and the server's access log are to join on it,
   the schema wants a normative home — doc 06 or a small doc of its own.
4. **A facets operation** (§10 item 10) as a doc 03 candidate, with its cost row.
5. **The closed-predicate declaration** (doc 14 §14.4) promoted into doc 04 §4.3
   beside `predicate_roles`, so C4 has something to read.
6. **`User-Agent` convention** for KGF clients (D11), in doc 06.
7. **Session pinning semantics.** Doc 06 §6.2.1 says `latest` is pinned per session;
   a CLI's "session" is a working-directory file. Worth one sentence so the MCP
   server and the CLI agree on when a pin expires.
8. **Doc 06 §6.2's hosted variant, and §6.2.1's receipt as a table comment.** D13
   withdraws the first — the sentence should go, with the reason. D15 changes the
   second to a sidecar plus Parquet footer metadata; doc 06 §6.7's "provenance
   attached to every local table" is then satisfied by the file rather than the
   database.
9. **Provider-run servers as the stated topology for clients.** Doc 05 §5.1 names
   it; doc 06 still reads as if a client faced one deployment. D14's rules — the
   graph as the unit, per-endpoint caps and capabilities, joinability from shared
   identifier spaces — belong in doc 06 so every client, not only this one, is built
   for it.
10. **Doc 05 §5.6.4, confirmed from the client side.** The template compilation it
    describes is D16 here; the version signal and `retrieved_at` its §5.6.2 records
    are what a `sparql` receipt carries (D17). Doc 06 should name the four source
    grades so every client reports them the same way.
11. **A bounded SPARQL tool for agents, and the ceiling on the intent document.**
    Raised 2026-09-10, after phase A closed, from a worry put plainly: the goal is a
    client surface that makes agents supremely effective at finding, querying, combining
    and deriving from OKN graphs, and three things in this plan could work against that.
    *(i)* The multi-pattern intent (C4, `notes/intent.md`) risks becoming a second
    SPARQL — a bespoke JSON that no model has seen, validated by nothing but our schema —
    the moment it grows OPTIONAL, UNION, filter expressions or property paths; doc 06
    §6.5 already says "LLMs know SPARQL; bespoke JSON is a second language for them".
    Proposed ceiling: an intent is a list of fetch steps, each pattern optionally bound
    by a column of an earlier step, and nothing more; combination is SQL over tables,
    and anything richer is SPARQL through the engine. *(ii)* The library must not grow
    Comunica's join taxonomy. Its one join-shaped decision is R2 — fetch a pattern whole
    or push a column as bindings — chosen from counts the server reports exactly, where
    Comunica must estimate (and mispriced paged sources until kgf-sparql's 2026-09-10
    fixes); relational joins are DuckDB's. §5.5's "about twenty lines" is the invariant.
    *(iii)* D16 and §5.4 exclude a `sparql` tool from `kgfq-mcp` on doc 12 §12.5.3's
    evidence — a passthrough carried 86% of an agent's traffic and the tail died in the
    client. That evidence predates the budget, the receipt, the native source and the
    linter. With those in front of it, SPARQL is a bounded operation whose result lands
    as a graded table: the corpus completes 58 of 94 queries under a 5,000-request budget
    and every failure is a receipt (§9), and federation across graphs runs through it
    today. The remaining objection is about the agent's choice — that it reaches for
    SPARQL first and decomposes never — which a skill and the refusal diagnostics address
    and absence does not; absence moves the agent to a shell. Proposed: expose `sparql`
    in `kgfq-mcp` exactly as the CLI does (lint with refusal, budget, receipt, table) and
    let §8's evaluation measure whether agents choose well between it and decomposition;
    treat the multi-pattern intent as batching and replay, dropped if fetch steps plus
    SQL prove enough. Changing (iii) changes D16 and §5.4 and must be recorded there; for
    the docs, doc 06 §6.5 and doc 12 §12.5.3 would then describe a bounded tool rather
    than a banned one.
12. **Doc 06 §6.3's column set should say how a table tells an IRI from a plain
    literal.** It names `s, p, o, o_lang, o_datatype` and no encoding. kgfq added
    `o_kind` (C6, changed 2026-09-24) rather than spell out `xsd:string` on every
    plain literal. When the server ships Parquet (D8), the same question decides whether
    it sends a kind column, or a spelled-out datatype the client strips.
13. **Doc 04's summary card lists "top classes", and a size ranking hides small
    reference classes.** SCALES' 94 `jxdm:Court` entities, with their ZIP codes and
    kwg county links, are 30th of 31 classes and absent from its card; an agent choosing
    sources from cards never finds them. Filed as kgf-rs#21 (2026-09-24) with the
    proposal: list every class when a graph has at most ~50 (32 of the 44 live graphs),
    and above that say how many are left out. Doc 04 may want the rule stated.

## 12. Non-goals

No SPARQL engine of our own. No Comunica fork. No general query rewriting. No
server-side code. No hosted MCP variant, in any release (D13) — a server running
joins for its users is the cost structure KGF exists to escape. No grouping of graphs
by the server that hosts them (D14). No column-level lineage through arbitrary SQL —
`cite` is a lookup over the workspace, not a lineage engine (C11). No TypeScript
client library in the first release — `kgf-sparql` is the TypeScript-world entry
point. No web UI — `kgf serve` already renders one. And no `sparql_query` tool,
however often it is asked for — not against a KGF graph and not against a SPARQL-only
one; the templates of D16 are code's.
