---
name: verbalize-config
description: Design and evaluate a verbalization config for a knowledge graph served by KGF, using kgfq — the inventory of what each class is made of, sampled texts read as a person would, and a report that cites the workspace.
---

# Design a verbalization config for a knowledge graph served by KGF

The deliverables are a **verbalization config** (YAML) and a **report** arguing
every choice from evidence. The config decides which RDF nodes become embedding
documents, how each is labelled, which edges contribute text, and which regions are
skipped; `kgf vectors --config` consumes it and publishes it in the bundle.

The graph is served, not local. Every read is a `kgfq` command against a KGF
server; you never need a graph file or a build. Every command leaves a record in
the working directory — `kgfq.log.jsonl`, and a table with a receipt for anything
you `--out` — and the report cites those rather than pasting numbers.

Pass `--table` to every command: it keeps the aligned-text form when stdout is a
pipe, which is what you read. Name the server once with `kgfq endpoints add
<origin>` (or `--endpoint <origin>` per command), then refer to graphs by id.

## How verbalization works

For each configured class (a **target**) the verbalizer takes the class's
subjects as roots, gives each a label, walks its outgoing edges, and renders one
`predicate label: value label` line per predicate/value pair, predicates in IRI
order. Object IRIs are shown by their labels, never expanded. A predicate
contributes at most `predicate_limit` values; past that a stable uniform sample
survives — reproducible, not "the first" or "the best". The label predicate is
walked like any other, so unless excluded the label line prints twice. Roots with
identical text are grouped into one record. Lengths are characters; the embedding
window is ~256 word pieces, roughly 1,000 characters of prose and fewer for
identifier-heavy text, so keep texts well under that.

## The config

```yaml
predicate_limit: 3                 # values per predicate; default 3
defaults:                          # merged into every target; lists appended without duplicates
  label_predicates: [...]          # tried, in order, BEFORE the bundle's own label cascade
  ignore_predicates: [...]         # left out of the walk
  include_predicates: [...]        # when non-empty, the ONLY predicates walked; ignore still applies within
profiles:                          # label templates keyed by class; apply to a node of that class in ANY position
  <name>:
    type: <class IRI>
    template: "Location {id}"      # {field} placeholders
    fields: {id: <predicate IRI>}
targets:
  <name>:
    type: <class IRI>              # required; everything else optional
    label_predicates: [...]
    ignore_predicates: [...]
    include_predicates: [...]
    predicate_limit: 5             # overrides the document value for this target
    label_template: "Site {id}"    # applies ONLY when the node is the root of a record
    label_fields: {id: <predicate IRI>}
```

**Every IRI is absolute.** The config is not expanded against the graph's
prefixes: a CURIE is an unknown IRI and a target with zero members. The inventory
gives you absolute IRIs to paste for exactly this reason.

The server validates the config, not you. A key it does not know is a 400 that
names the key and the line; an IRI the graph does not hold is an `unknown[]` entry
with the config path it sits at — `kgfq verbalize` prints those first. A typo is
the commonest mistake; fix every one before reading anything else.

Two label mechanisms because they answer different questions: a **profile** names
a node of a class wherever it appears (as a root, or mentioned in another root's
text); a target's **`label_template`** names the root only, so a rich
self-description does not leak into every document that mentions the class. Root
label resolution: the target's `label_template`, else a profile for its class,
else `label_predicates`, else the bundle's declared label cascade, else the IRI's
humanized fragment. A mentioned node: profile, cascade, fragment. You do not need
to read the declared cascade; the server already puts it behind yours.

`ignore_predicates` is a denylist, `include_predicates` an allowlist. Choose by
which list is smaller and more stable: a graph with a handful of noisy predicates
wants a denylist; a graph whose relation vocabulary is large or open-ended wants
an allowlist — a denylist silently lets new noise back in.

## The commands

| | |
|---|---|
| `kgfq datasets --table` | every graph the endpoints serve, with triple counts |
| `kgfq summary <ds>` | the graph's summary card: what it is about, its biggest classes and predicates |
| `kgfq inventory <ds> --table` | **start here.** One row per (class, predicate): `entities` of the class, `distinct_subjects`, `triples`, `distinct_objects`, `coverage` (`distinct_subjects / entities` — how many of the class carry it), `fanout` (`triples / distinct_subjects` — values per subject that has one), and `object_classes` (what IRI values point at; empty means literals). Biggest classes first, then within a class the predicates most of its entities carry. Three requests the first time, none after. |
| `kgfq inventory <ds> --out inventory` | the same table filed with a receipt, so the report can cite its rows |
| `kgfq sample <ds> '? rdf:type <class IRI>' -n 5 --seed 1 --table` | seeded members of a class; the same seed draws the same members |
| `kgfq verbalize <ds> --config config.yaml -n 3 --seed 1 --table` | **the main tool.** The plan: per target `members`, `sampled`, length statistics, `truncated`, `limited`; then every sampled root's text. `--target <name>` samples one target; `--target <name> --iri <IRI>` renders exactly that root (repeat `--iri`) — the node as the embedding sees it. `--out plan` files the per-target figures. |
| `kgfq fetch <ds> '<node IRI> ? ?' --out node` | a node's raw outgoing edges, filed as a table (`s, p, o, …`) with a receipt, when you need the IRIs the config would drop |
| `kgfq log --table`, `kgfq receipt <table>` | what you ran, and what a filed table came from |

The root cap is a server rule over the whole plan — `targets × n` may not exceed
its `max_verbalize_roots` — and its refusal says what to do: lower `n`, or name one
target. Every request is bounded by the server; that is the point.

**To look at a node, verbalize it rather than fetch it.** A one-target config —
`targets: {x: {type: <its class>}}` — with `--target x --iri <node>` shows the node
as the embedding would, which is the view you are tuning anyway, in a tenth of the
bytes. Use `fetch` only when you need the raw IRIs.

## Workflow

1. `kgfq datasets --table`, then `kgfq summary <ds>`: what the graph is about.
2. `kgfq inventory <ds> --out inventory`, then `kgfq inventory <ds> --table` to read
   it (the second costs no request). Record every class's entity count and what it
   appears to represent — from its name if legible, otherwise from its predicates'
   `object_classes` and `distinct_objects`, and from sampled members. A class whose
   predicates are one number, one unit link and one type is a value node whatever
   its name.
3. Identify candidate root types. Prefer entity-like types that produce compact,
   meaningful documents. Avoid structural/helper types, extremely numerous
   measurement/value nodes, geometries, units, blank-node containers, and classes
   with only ontology statements (unless the whole graph is an ontology).
4. For each candidate read its rows: `coverage` for how many members carry a
   predicate, `fanout` for how many values each gets. High fanout alone does not
   mean "ignore" — ask whether the values are *exchangeable*:
   - **Exchangeable** — siblings of one kind, none more correct than another (the
     counties of a state, the authors of a paper). A sample is informative;
     `predicate_limit` is the right tool. Keep the predicate.
   - **Ranked** — values differing in informativeness, typically a transitive
     hierarchy or closure. A sample almost always misses the useful one and reads
     as generic noise. Exclude the predicate.
   `object_classes` tells you which helper classes a target mentions; those are the
   candidates for a profile.
5. Sample 3–5 members of each candidate and verbalize two of them under a draft
   one-target config, to see real values and spot: predicates carrying identical
   boilerplate, long literals, geometry blobs, links to unlabelled helper nodes.
6. Draft the config. Set target `type`s explicitly. Prefer `label_predicates` when
   a real name/title/label predicate exists; use a `label_template` or `profile`
   only when direct labels are absent, weak, or verbose — and check every template
   field's `coverage` first (a template whose fields are missing on most roots
   renders as debris). Keep `rdf:type` in the text by default; drop it when a
   target's class is the only type its members carry (every record would say the
   same thing). Exclude the label predicate from the walk so the label is not
   printed twice. Set a conservative `predicate_limit`.
7. `kgfq verbalize <ds> --config config.yaml -n 3 --seed 1 --table`. Fix every
   `unknown`. Read each sampled text as a person would. Iterate: exclude noise, add
   templates, adjust limits. Re-plan after every change; watch `mean_chars` and
   `max_chars`, and `limited` (predicates sampled down — the number to tune the
   limit or the lists by).
8. Stop when the sampled texts are compact, readable, and semantically useful for
   every target. Run the final plan once with `--out plan`.
9. Write `README.md`: every class considered with its count and a one-line
   summary, citing rows of `inventory`; targets chosen and why; skipped types and
   why; for each target its label strategy, excluded predicates and why, and two
   sample texts; an opinionated wrap-up (strongest and weakest representations,
   what to reconsider, next iterations); remaining uncertainties; and a short
   final section, **What slowed you down**: anything about the server, its
   responses, `kgfq`, or these instructions that was confusing, missing, wrong, or
   cost you requests. The requests you ran are `kgfq log`; do not retype them.
   Personal data (e-mail, phone, addresses) in a target's text is always a
   question for that section, never a decision made here.
10. Write the final `config.yaml`.

## Asking for guidance

Prefer server evidence for routine decisions. Do not ask about facts the server
can answer. Stop and ask (in your final report, as open questions) when a decision
depends on domain intent — whether a borderline class is a target, a helper, or
skipped; whether a high-volume class is worth embedding; what users will search
for.

## Quality bar

- Do not try to embed everything. Each target is a thing a user may search for.
- Texts concise enough for a small sentence-transformer model.
- Labels stable and human-scannable.
- Every non-obvious choice backed by a number or a sampled text in the report.
- Every target represented in the report by rationale, count, summary, and sample.
- The wrap-up is opinionated, based on the sampled documents.
