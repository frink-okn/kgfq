# kgfq — project norms

The client half of Knowledge Graph Fragments: one Python library, thin `kgfq` and
`kgfq-mcp` skins. `notes/plan.md` is the design of record; read its §0–§3 before
changing anything, and the unit (C1–C11) or decision (D1–D17) a change touches.

## Toolchain: uv, ruff, ty

- **uv** owns the environment and the lock. `uv sync`, `uv run <cmd>`, `uv add`.
  Never `pip install`; never edit `uv.lock` by hand. Python 3.12+.
- **ruff** lints and formats; config in `pyproject.toml`. **ty** type-checks.
- Before calling any change done, all four must be clean:

```bash
uv run ruff format src tests
uv run ruff check src tests
uv run ty check src tests
uv run pytest
```

- No `# noqa` or `# type: ignore` without a trailing reason on the same line.
- Type annotations on every public function; `ty` must pass without suppressions.

## Tests: real servers, never mocks

- Golden tests spawn two real `kgf serve` processes (one graph each, D14) over
  bundles built at test start by `kgf build` from `tests/fixtures/*.nt` — see
  `tests/conftest.py`. Binaries come from `KGFQ_KGF`/`KGFQ_HDTC` or the sibling
  checkouts' release builds (`../kgf-rs/target/release/kgf`,
  `../hdtc/target/release/hdtc`).
- Never mock the server. The traps this library exists to absorb were all found
  against a live one.
- `kgf manifest` is archaic: only `kgf build`. Do not use `../kgf-rs/demo` bundles;
  they are stale. Probe a freshly built fixture to learn a wire shape.
- Every unit in the plan has a *Verified by* line; a landed unit's tests must cover it.

## Architecture rules (from the plan; do not relitigate silently)

- One library, several skins. `kgfq/cli` and `kgfq/mcp` contain no planning logic;
  operation selection lives only in `kgfq/plan` (D3). A rule that exists twice
  diverges, and the measured cost is three to four orders of magnitude.
- No `sparql_query` tool, ever, and no model-authored SPARQL against any source
  (D2, D16, §5.4). SPARQL-only graphs are reached through templates that are code's.
- The workspace is the product (D15): every library operation appends one record
  to `kgfq.log.jsonl`; every table gets a receipt beside it and in its Parquet
  footer; every receipt carries a source grade (D17). DuckDB is the engine, not the
  store; nothing but `kgfq/workspace` (C8, when it lands) opens one.
- Fetched tables always have the columns `s, p, o, o_kind, o_lang, o_datatype`
  (plus `binding`), whatever the caller named its variables — every fetched table
  is a set of triples. `o_kind` states iri, literal or bnode; a plain literal has
  no datatype stored (never `xsd:string` spelled out).
- Caps and budgets come from each endpoint's service descriptor; never hard-code
  one. State is per endpoint; the session is a set of graphs, never grouped by
  server (D14).
- Errors from a server are its problem document, verbatim. Never paraphrase.
- Local-only (D13). Do not propose a hosted variant.
- A skill in `skills/` carries method, not server facts: a field name, a cap, a
  label rule, a config key belongs in a command that renders it or in the server's
  validation, never in prose an agent copies. `.claude/skills/` symlinks each one.

## Keeping the plan current

- When a unit lands, add a *Landed <date>* paragraph under its heading saying what
  was decided while building it. When a decision changes, change the D-item and
  say why; decisions are surfaced, not quietly replaced.
- Questions for the design docs (`../kgf/docs`) go in plan §11. Edits to `../kgf`
  and any commit happen only when asked.
- The intent representation (C4) is open; `notes/intent.md` is the proposal.

## Style

- Docstrings name the plan unit or decision a module implements, and why it is
  shaped the way it is, in the plan's own terse register.
- Match the existing voice in `notes/` and `docs/`: short declaratives, cross-refs
  to doc and section numbers, measurements over adjectives.
- JSON schemas for plan, receipt, and log record live in `docs/`; change the schema
  and its `schema` version string together.

## Neighbours

```
../kgf          design docs (doc 03 = API, doc 05 = federation, doc 06 = clients)
../kgf-rs       the server and `kgf build` (Rust)
../hdtc         the HDT toolchain `kgf build` drives
../kgf-sparql   Comunica + KGF actor suite; launched as a subprocess, never linked
../mcp-okn      the pre-KGF MCP server; its benchmark is the acceptance suite
../okn-registry kgs.yaml and the 89 registry queries
```
