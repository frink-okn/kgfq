# The session format — a proposal

Proposed 2026-11-20 for issue #3 (parity and compatibility with `../kgf-explorer`), accepted
the same day, and landing in the order of §10; each part says when it landed. It changes one
decision of record, C6's "every fetched table is a set of triples" (§4.3).

## 1. The problem

Two clients write sessions: kgfq (`kgfq.log/1`, `kgfq.receipt/3`, a workspace directory) and
kgf-explorer (an unschema'd log record, `kgf-explorer.receipt/0`, tables in a tab). The
explorer's plan §0 already wants "a session explored here … taken home to kgfq", and its §3
item 3 names kgfq's schemas. Wanted, in order:

1. A session written by either client loads in the other: its steps, its tables, its receipts.
2. The explorer shows a kgfq session. Showing one live, as a harness writes it through
   `kgfq-mcp`, is later work (§11).
3. Neither client has to move in lockstep with the other. One gaining an operation the other
   lacks must not break loading, in either direction.

The third is the design constraint. Two schemas that must be edited together drift; the
measured cost of a rule that exists twice is in kgfq §0.

## 2. Principles

1. **An envelope and a payload.** Every record has a small shared envelope that every reader
   must understand, and a per-operation payload that only clients implementing the operation
   need to understand. Receipts split the same way (§5).
2. **An unknown operation is normal.** A reader that does not implement an operation still
   shows its step, loads its tables, uses them in SQL and carries their grades. It cannot
   replay that step, and says so by name ("step 7: `overlap` is not replayable by kgfq 0.1").
   Replayability is per operation and per client.
3. **Unknown fields are ignored, never refused.** The envelope schema has no `const` and no
   closed enum except the major version. An added field is a minor version; only a change to
   what an existing envelope field means is a major one. `kgfq.log/1`'s `const` schema string
   and closed `skin` enum are what would force lockstep, and go.
4. **An operation name means one thing everywhere.** One registry (§6) gives each operation
   its meaning and its args schema. Registering an operation does not oblige any client to
   implement it. An operation only one client has is still registered, so a name is never
   reused and never renamed when the other client catches up.
5. **A table declares its shape** (§4). No reader infers a table's layout from its columns.
6. **The conversation is outside the log.** An explorer transcript (its D18) and a harness's
   chat refer to steps by number. An agent's final claims enter the log only through a
   registered operation (`answer`, §8), never as a copy of the chat.

## 3. The envelope

*Landed in kgfq 2026-11-20* as `kgf.session/1.0` (`kgfq.workspace.log.Step`,
`docs/session/envelope.schema.json`): every record kgfq writes is validated against it and
against the registry in `tests/test_session_format.py`.

| field | what it is |
|---|---|
| `schema` | `kgf.session/<major>.<minor>`. A reader accepts its own major and warns on a newer minor. |
| `step` | the record's number in the session, from 1. Other records name it: an input `derived` from step 3. |
| `started`, `elapsed_ms` | when the step started (RFC 3339, UTC) and how long it took. |
| `client` | `{name, version, surface}`: `kgfq` with `cli`, `mcp` or `library` (today's `skin`); `kgf-explorer` with its build (`git describe`) and `page`. |
| `actor` | who asked: `user`, `assistant`, `page` (the explorer's map steps, D19), or `null` where the client cannot tell. |
| `operation` | a registry name (§6). |
| `args` | the operation's arguments, in its registry schema. Payload, not envelope: a reader that does not know the operation keeps them verbatim and shows them as JSON. |
| `inputs` | each consumed value or table, classified `derived` (with `step` and `table`), `asserted`, or `external` (with `sha256`), as kgfq C9 does now. |
| `pins` | every graph the step read: `origin`, `dataset`, `version`, `content_digest`, `dataset_iri`. |
| `outputs` | tables the step wrote: `table`, `shape`, `rows`, `grade`, `content_hash`; `path` only for one written outside the session's `tables/`. Empty for a step that wrote none. |
| `result` | what a step answered without a table: a count, an ASK, how a run ended. Payload, as `args` is. Added while building: kgfq's `count` writes no table, and its answer is the step's evidence. |
| `status` | `ok` or `error`. |
| `error` | the server's problem document (RFC 9457) verbatim, or the client's refusal as text when no request was made. |
| `receipt` | `{schema, core, detail}` (§5), whole: the one the table's footer carries, or the only copy for a step with no table. |

```json
{"schema": "kgf.session/1.0", "step": 4, "started": "2026-11-20T15:02:11Z", "elapsed_ms": 812,
 "client": {"name": "kgf-explorer", "version": "df21dc2", "surface": "page"}, "actor": "assistant",
 "operation": "fetch",
 "args": {"graph": "ruralkg", "subject": "?prov", "predicate": "<https://schema.org/postalCode>", "object": "?zip"},
 "inputs": {},
 "pins": [{"origin": "https://apps.okn.us/kgf", "dataset": "ruralkg", "version": "2026-04-02", "content_digest": "…", "dataset_iri": "…"}],
 "outputs": [{"table": "t4", "shape": "bindings", "rows": 5420, "grade": "bundle", "content_hash": "sha256:…"}],
 "status": "ok",
 "receipt": {"core": {"…": "…"}, "detail": {"…": "…"}}}
```

**The server is in every step.** A pin names the origin per graph, so a step over two graphs on
two servers names two origins (kgfq D14). The digest is the identity and the origin is where it
was fetched. Replay goes to the recorded origin by default; any origin serving the same dataset
with the same digest is equivalent, so a moved graph is replayed elsewhere and checked by
digest. Replaying against a newer release is re-planning, a separate mode (C10).

## 4. Tables

### 4.1 Storage

A session is a directory, zipped to share:

```
kgfq.log.jsonl            the steps, one record per line (§3)
tables/
  t4.parquet              a table's rows, its receipt in the footer under `kgf.receipt`
  t4.receipt.json         optional: the same receipt as a file
```

**The log and the Parquet files are the format; a reader needs nothing else.** The footer is
a table's authoritative receipt, since it cannot be separated from the rows. Every log record
carries its step's receipt too, which is where a step that wrote no table (an aborted SPARQL
run) keeps its own. The sidecar is a convenience for `cat` and `jq`; a client may write it and
no reader depends on it. Today kgfq's `read_receipt` prefers the sidecar to the footer, which
reverses. `.kgfq/` is caches and scratch and is not exchanged.

A table name is a string unique within the session: the explorer's `t4` and kgfq's `genes`
are both names, and `bind` refers to either.
Term columns use the encoding both clients share already (kgfq C6 2026-09-24, explorer D6): a
column `x` with `x_kind` (`iri`, `literal`, `bnode`), `x_lang` and `x_datatype`; a plain
literal stores no datatype, and a blank node keeps its `_:` (`kgfq.client.terms`, the
explorer's `src/terms.js`).

### 4.2 Shapes

| shape | columns | produced by | carries |
|---|---|---|---|
| `bindings` | one term column per variable, with its companions | `fetch`, `count` with bindings, the templates, SPARQL SELECT, `sql` | `pattern` when the table answers one pattern; absent otherwise |
| `triples` | `s, p, o, o_kind, o_lang, o_datatype` | SPARQL CONSTRUCT and DESCRIBE, `import` of RDF | — |
| `records` | plain columns, no term companions | listings: `datasets`, `schema`, `distinct`, `overlap` | — |

Whether `records` should be a shape, or listings should carry term columns where they hold
terms (a schema listing's classes are IRIs), is open (§11).

### 4.3 Fetch tables are bindings (changes kgfq C6)

A `fetch` table is stored as `bindings` with its pattern in the receipt, in both clients.
kgfq stored `s, p, o, …`; the explorer stores the pattern's variables. *Landed in kgfq
2026-11-20* (`kgfq.plan.fetch.fetch_variables`, receipt `kgfq.receipt/4`; plan C6 records
what was decided while building).

For the pattern `?prov <https://schema.org/postalCode> ?zip`:

| prov | prov_kind | prov_lang | prov_datatype | zip | zip_kind | zip_lang | zip_datatype |
|---|---|---|---|---|---|---|---|
| …/provider/17 | iri | | | 27514 | literal | | |

Two rules make it lossless:

- **Every variable position has a column, anonymous ones included.** An anonymous position is
  named after itself (`s`, `p`, `o`), as the explorer's `position()` does now. A name already
  taken by a pattern variable is a refusal, never a silent self-join: `as_body` named
  anonymous positions the same way and turned `?o <p> ?` under bindings for `?o` into one
  (plan §6, closed 2026-11-20). Whether the explorer refuses that collision is unchecked
  (§11).
- **A constant position has no column** and is kept in the pattern. So a pattern with no
  variable has no table, and is refused: `count` answers it.
- **Columns do not collide.** `?c` and `?c_kind` would both need `c_kind`; refused.
- **A repeated variable is one column**, and the equality is the server's to apply, which it
  does only when the bindings bind the variable in every row. Without bindings it is
  refused, since a GET sends no names (plan §6, 2026-11-20).
- **Input tuples are distinct.** A bindings block joins like `VALUES`; a tuple sent twice
  would answer twice, and no column tells the copies apart.

`triples(table, receipt)`, about twenty lines in each client, puts each row's values back into
the pattern and returns the triples. It is lossless for a one-pattern table, and both clients'
copies are tested against the shared fixtures (§9). `cite` (C11) and RDF export
(`kgfq export <table> --format nt`) use it. *Landed in kgfq 2026-11-20*
(`kgfq.workspace.triples`): an export of `? ? ?` from the fixture server parses to the graph
the bundle was built from.

Why bindings and not triples:

1. **The model reads it.** The explorer's prompts and tool descriptions were tuned on runs with
   variable names (`t3.zip = t5.zip`, not `t3.o = t5.s`), and kgfq-mcp's model will read the
   same tables. Projecting on every read would be work in both clients, permanently.
2. **It is the general shape.** A SPARQL SELECT result is already bindings in kgfq (C6
   2026-09-10). With fetch as bindings, a table of fetched data has one layout, not two, and
   `? ? ?` gives columns named `s`, `p` and `o`, triples by another name.
3. **The objection is answered by the footer.** A bindings row means nothing without its
   pattern; the pattern is in the receipt, the receipt is in the file, so the file is still
   self-describing.
4. **Parity first.** The explorer's tables stay as they are, and kgfq, whose tables have fewer
   readers, changes.

What it costs kgfq: `fetch` output and its tests; content hashes, so tables written before do
not match (as with `o_kind`); `classify_values`, which reads only `s` and `o` and would read
every term column; C6 and the `CLAUDE.md` rule, rewritten with a *Changed* note. `cite` is not
built, so it starts on `triples()`. The `binding` column, the index of the input tuple a row
answered, goes: a bound variable is a column, so the unmatched report (C6) is the input tuples
whose values no row holds.

## 5. Receipts

*Landed in kgfq 2026-11-20* as `kgf.receipt/1.0` (`kgfq.workspace.receipts.new_receipt`,
`docs/session/receipt.schema.json`): `{schema, core, detail}`, validated on every receipt the
tests write, and a receipt with an unknown operation and unknown fields read back whole.

**Core**, shared, read by every client:

- `kind` (the operation that wrote it), `step`, `created`.
- `grade`: how far the table can be checked later, kgfq D17's four values. `bundle`: read
  from a KGF release, re-fetched identically by digest. `sparql`: a live endpoint, re-run and
  compared. `external`: brought in from elsewhere, checked only against the archived copy.
  `asserted`: typed by a person or a model. A derived table takes the lowest grade among its
  inputs. Grades are the clients' convention, not the server's; the explorer follows the same
  rule (its D5), so the core states it once.
- `sources`: each graph or endpoint read, graded, with the pin fields of §3.
- `shape`, `columns`, `rows`, `content_hash`; `pattern` for a one-pattern `bindings` table.
- `completeness`: `complete`, `incomplete_because`, as `kgfq.receipt/3` defines them.
- `totals`: `requests`, `bindings_requests`, `bytes_decoded`, `bytes_wire` (null where a
  client cannot see the wire, as a page cannot), `elapsed_ms`.
- `requests`: each with method, URL, status, bytes and `KGF-Request-Id`; absent where the
  list is kept elsewhere and the detail points at it, as a SPARQL run's is.
- `bindings`, for a bound step: `table`, `columns`, `sent`, `matched`, `unmatched`,
  `unmatched_sample` (kgfq C6's unmatched report, explorer D8).
- `approximations`, and `inputs` for a derived table: the receipts of the tables it read, one
  hop (D17).

**Detail**, per operation: kgfq's plan (D12), statistics and `requests_by_shape`; the
explorer's budget, engine versions and notes; a `sparql` receipt's lint and normalization
record and its pointer to the engine receipt. A reader that does not know the operation keeps
the detail verbatim.

**The content hash is specified here, not by either implementation.** *Landed 2026-11-20*
(`kgfq.workspace.store.TableHash`, vectors in `tests/test_workspace.py`). A row is its cells
in column order, each self-delimiting:

| cell | bytes |
|---|---|
| null | `N` |
| string | `S`, the UTF-8 length in decimal, `:`, the UTF-8 bytes |
| integer | `I`, its decimal digits, `;` |
| boolean | `B1` or `B0` |
| bytes | `X`, the length in decimal, `:`, the bytes |

The rows' SHA-256 digests are added as 256-bit big-endian integers, modulo 2^256. The hash is
`sha256:` and the hex of SHA-256 over, in order: the preamble `kgf.table-hash/1` and a
newline; the column names as one row of strings; `R`, the row count in decimal, `;`; and the
sum as 32 bytes.

Why this shape:

- **It is taken a row at a time, in any order, in constant memory.** A sum does not care
  about order, so nothing is sorted and nothing is held: a fetch can hash its pages as they
  arrive, and a table larger than memory can be hashed. Whole-predicate fetches of 41 M
  triples have been seen holding 9 GB; a hash that needs every row at once would stand in
  the way of streaming them to disk. A repeated row counts each time, since a table is a
  bag of rows.
- **Nothing depends on a language.** The first version was compact JSON per row, sorted as
  strings: Python sorts strings by code point and JavaScript by UTF-16 unit, which differ
  above U+FFFF, and `json.dumps` and `JSON.stringify` would have had to agree on every escape
  and every number. Lengths need no escaping, and an integer sum is the same everywhere.
- **A column's type is part of the table**: the integer 1 and the string `1` hash apart. A
  float has no spelling two languages share and is refused until one is chosen (§11); the
  explorer returns numbers as typed literals, which is one answer.
- **It finds any difference that happens; it is not proof against a table built to
  collide.** A sum of digests can be attacked where a single digest cannot. It need not
  resist that: whoever can rewrite a table's rows can rewrite the receipt in its footer.

## 6. The operation registry

`docs/session/operations.yaml`, one entry per operation. *Landed 2026-11-20* with the 33
operations the two clients have between them (`tests/test_session_format.py` checks its
form, and that each name kgfq is said to have is a command it runs).

```yaml
fetch:
  meaning: >-
    Every triple matching one pattern in one graph, as a table whose columns are the
    pattern's variables. Under bindings, the rows the input tuples reach. A constant has
    no column; the receipt's pattern keeps it.
  args:                     # JSON Schema
    type: object
    required: [graph]
    properties:
      graph: {type: string}
      subject: {type: string}
      predicate: {type: string}
      object: {type: string}
      bind:
        type: object
        required: [table, columns]
        properties:
          table: {type: string}
          columns: {type: object, additionalProperties: {type: string}}
      values: {type: string}      # kgfq: a bindings file
      max_rows: {type: integer}   # kgfq
      out: {type: string}         # kgfq
  table: bindings
  implemented_by: {kgf-explorer: fetch, kgfq: fetch}
```

Decided while writing it:

- **The names are the explorer's** where it has the operation: `list_graphs`,
  `graph_summary`. Its names are the ones models were measured on (its D18), its tool list is
  signed into the gate's manifest, and `kgfq-mcp`'s tools will want the same names for the
  same reason. `implemented_by` maps each client to the name the operation goes by there, so
  `kgfq summary` is logged as `graph_summary` and the command keeps its name.
- **`graph`, not `dataset`**, and a pattern as `subject`, `predicate`, `object`: each a term
  in doc 03 §3.3 syntax with IRIs bracketed, `?name`, or left out for an unnamed position.
  A client logs that spelling whatever its surface took; the explorer, which accepts a bare
  `http…` IRI in a pattern, brackets it on the way to the log.
- **`bind: {table, columns}`** is the explorer's form and the registry's. kgfq has only
  `--values <file>` until it has `bind` (§10 step 4); the registry lists `values` as kgfq's,
  and the record's `inputs` classify the file. `import` (C9) will make the file a table.
- **Nothing is closed.** A client may log an argument the registry does not list, and a
  reader keeps it.
- **No `replayable_by` yet.** Replay is per operation and per client (§2), and no client
  replays anything until C10; the field arrives with it.
- **`table`** is stated only where the shape is settled. A listing's is open (§11).
- **`answer` and `import` are not registered**: neither is designed. An entry needs its
  arguments.

`implemented_by` is the parity record. §8 is a snapshot of it with the reasons beside each
row; where they differ, the registry is right. The registry holds meaning and arguments, not
tool descriptions: those are each surface's own and are measured by its own runs.

## 7. Today's records, mapped

| envelope | `kgfq.log/1` | explorer log record |
|---|---|---|
| `schema` | `"kgfq.log/1"` | none |
| `step` | `step` | `seq` |
| `started` | `started` | `at` |
| `elapsed_ms` | `elapsed_ms` | `receipt.totals.elapsed_ms` |
| `client` | `skin` | none; the transcript has the build |
| `actor` | none (`mcp` implies `assistant`; `cli` cannot tell) | `actor` |
| `operation` | `command` | `operation` |
| `args` | `args`, in CLI spelling | `input` |
| `inputs` | `inputs` | none; `bind` names the table |
| `pins` | `pins` | in `receipt.sources` (dataset, version, fragment URL); digest unchecked |
| `outputs` | `output` (`table`, `rows`, `content_hash`) | `table` (an id); no hash, no rows |
| `status` | `ok` / `error` | `running`, or the receipt's `outcome.contract` |
| `error` | `error` | `receipt.outcome.error` |
| `receipt` | `receipt` | `receipt` |

Changes this asks of each:

- **kgfq** (all landed 2026-11-20): `skin` becomes `client`; `actor` is added; `command`
  becomes `operation` and `args` takes registry spelling; the schema string changes; fetch
  tables become bindings (§4.3); a receipt splits into core and detail. Workspaces written
  before this are not read: none is being kept.
- **kgf-explorer:** the envelope fields its records lack (pins, inputs, outputs with hashes);
  the content hash; `status` separated from the receipt's contract; export and import (below).
  Its tables and its `bind` are unchanged.

**The explorer's export**, none of it tried. Nothing is written to disk by the page: DuckDB-Wasm
(1.32.0) writes each table with `COPY … (FORMAT parquet, KV_METADATA …)` into its in-memory
filesystem, `copyFileToBuffer` hands the bytes to the page, `fflate` zips them with the log,
and the zip is a Blob behind an `<a download>`, as its log export is now (`src/app.js`).
`showSaveFilePicker` is Chromium only and is not used. The page's DuckDB is locked before any
model SQL runs (`enable_external_access = false`, `lock_configuration = true`, its D11), and
the lock is one-way and database-wide. In order: (1) test whether `COPY` to an in-memory path
is refused under it; (2) if so, a second `AsyncDuckDB` (its own worker, the same cached
`.wasm`) made for the export and discarded, which model SQL never reaches. Not `allowed_paths`:
model SQL could write the same path. Import is reading a registered buffer and needs neither.

## 8. Parity

Registry names, with where each operation lives today. The "kgfq" column means the library
and CLI; `kgfq-mcp` follows the library. The registry's `implemented_by` is the record (§6).

| operation | kgf-explorer | kgfq | for parity |
|---|---|---|---|
| `list_graphs` | `list_graphs` (one server) | `datasets` (every origin) | have |
| `endpoints` | a page setting, not a step | `endpoints` | kgfq only, by design (D14) |
| `graph_summary` | `graph_summary` | `summary` | have |
| `schema` | graph, class, class+predicate | plus datatype, projection, view | have; args to reconcile |
| `search` | one graph's `/search` | — (C7 `resolve` planned) | **kgfq needs** |
| `count` | many graphs, bind from a table | one graph, `--values` | **kgfq needs** many graphs and `bind` |
| `sample` | — | `sample` | kgfq only for now |
| `distinct` | — | `distinct` (kgf-rs unit 38, not on `apps.okn.us`) | kgfq only for now |
| `fetch` | bind from a table | `--values`, `--max-rows`, R1/R2 plan | **kgfq needs** `bind`; bindings storage (§4.3) |
| `describe`, `incoming`, `instances`, `neighbors`, `two_hop` | SPARQL templates in the tab | — | **kgfq needs**, as fetch plans, not SPARQL: each is one or two patterns |
| `sql` | one SELECT, locked DuckDB-Wasm | — (C8 not built) | **kgfq needs** |
| `labels` | `/labels`, beside tables | — (C7 not built) | **kgfq needs** |
| `read_table` | rows for the model, paged | — | **kgfq-mcp needs** (handles, never rows, §7 of the plan) |
| `terms` | `/terms` counts and lists | — | **kgfq needs** |
| `overlap` | namespaces both ways | — | **kgfq needs** |
| `where` | an entity everywhere: the IRI, its Babel clique (the `babel` graph), same-label IRIs | — | **kgfq needs** |
| `connections` | an entity's predicates per graph | — | **kgfq needs** |
| `map` | the model's map, run by the page (D19) | — | explorer only for now |
| guided: `graphs`, `find`, `classes`, `members`, `links`, `follow`, `common`, `narrow` | D16 | — | explorer only; D16 found it not useful in use |
| `sparql` | never (explorer D2) | `sparql` | kgfq only, by design (§5) |
| `explain`, `lint` | — | both | kgfq only, by design |
| `import` | — | planned (C9) | kgfq first |
| `answer` | the reply, in the transcript | — | **new, both**: the agent's claims with `[t:genes#12]` citations, as a step (C11) |

## 9. Layout and fixtures

Temporarily in this repository, since `../kgf-dev` and `../kgf-explorer` are private:

```
docs/session/
  envelope.schema.json       §3
  receipt.schema.json        §5: the core, and that there is a detail
  operations.yaml            §6
  fixtures/kgfq/             a small session kgfq wrote: log, tables, receipts
  fixtures/kgf-explorer/     a small session the explorer exported
```

Each client's tests validate what it writes against the schemas, load the other client's
fixture, and reproduce its content hashes and `triples()`. The explorer has no test suite
(its §3 item 2); a Node script doing this is its first test. The explorer cites this
directory by commit until it moves.

*Landed 2026-11-20:* `fixtures/kgfq/`, seven steps against the two fixture servers, with
`expected/<table>.nt` holding the triples each table stands for, and a README saying what a
reader must be able to do with it and what each step is there to show. Its log is 18 kB and
its four tables 8 kB each. `tests/test_session_fixture.py` reads it with no server, and
reruns its commands against fresh servers to hold that it is still what kgfq writes,
comparing everything but times, request ids, ports and the bundle's digest.
`fixtures/kgf-explorer/` waits for the explorer's export (§10 step 6).

Opening a session: a zip of the workspace, a chosen directory, or
`?session=<url>` naming a zip the user put somewhere. Sharing is passing the file.

## 10. Sequence

1. This note, agreed. *Done 2026-11-20.*
2. `docs/session/`: the schemas, the registry from §8, and a kgfq fixture. *Done 2026-11-20.*
3. kgfq writes `kgf.session/1`: the envelope, receipt core and detail, bindings fetch tables
   (C6 changed), the specified hash, `triples()` behind `kgfq export`. No reader for
   `kgfq.log/1`: no workspace written before this is being kept. *Done 2026-11-20.*
4. kgfq parity, one operation at a time, in the order a harness needs them: `bind` for
   `fetch` and `count`, `sql` (C8), `read_table`, `labels`, `search`, the template operations,
   `count` over many graphs, `terms`, `overlap`, `connections`, `where`, `answer`.
5. `kgfq-mcp` over the library as it then stands (issue #2).
6. kgf-explorer, on a branch: export and import of `kgf.session/1`, its fixture, its test.

## 11. Open, and later

- **A refused step.** kgfq writes no record for a step a server refuses, or that it
  refuses itself before any request; the explorer records both. An agent's failed attempts
  are part of what happened, and the envelope has `status` and `error` for them.
- **`list_graphs` as a step.** `kgfq datasets` writes no record; the explorer's
  `list_graphs` does.
- **Multi-server sessions in the explorer.** It talks to one server. Loading a kgfq session
  whose graphs live on two servers needs it to hold several; after kgfq reaches parity, so
  that the two clients do not change at once.
- **RDF export in the explorer**, through its `triples()`.
- **`records` as a shape** (§4.2), or term columns in listings.
- **The explorer's anonymous-position collision**: an empty subject is `?s`, so an object of
  `?s` would make `?s <p> ?s`. Unchecked whether it refuses.
- **The explorer's pins**: whether every receipt records the content digest.
- **Numbers in a derived table's hash** (§5).
- **Labels in an exported session**: the explorer fetches them per table and keeps them beside
  it; whether an export carries them as a sidecar or the reader refetches.
- **A live view**, after §10. kgf-explorer, served by a `kgfq view` that serves a built
  explorer and the workspace read-only from one local origin, with an event stream announcing
  each appended record. It must work in Safari and Firefox, which rules out
  `showDirectoryPicker` (Chromium only); a directory chosen through a file input works
  everywhere but is a snapshot. One origin means no CORS and no mixed content. Local only
  (D13). How kgfq gets a built explorer is a packaging question.
- **Where the format lives** once the private repositories are settled: `../kgf-dev` as a doc
  06 appendix is the natural home.
