# A kgfq session, kept as the format's fixture

A small session `kgfq` wrote, in the format `notes/session-format.md` describes and the
schemas beside this directory define. A client that reads the format tests itself against it.

```
kgfq.log.jsonl        seven steps, one record a line (../../envelope.schema.json)
inputs/chems.tsv      the bindings file step 4 was given
tables/*.parquet      four fetched tables, each with its receipt in its footer under `kgf.receipt`
tables/*.receipt.json the same receipts as files; a reader needs none of them
expected/*.nt         the triples each table stands for
```

What a reader must be able to do with it, with no server:

1. Validate every line of the log against the envelope schema, each record's `receipt` and
   each table's footer against the receipt schema, and each record's `args` against its
   operation's entry in `../../operations.yaml`.
2. Recompute each table's content hash, in the `kgf.table-hash/1` encoding
   (`notes/session-format.md` §5), and get the `content_hash` its record's `outputs` and its
   footer give.
3. Turn each table back into triples, by putting each row's values into its receipt's
   `pattern`, and get the lines of `expected/<table>.nt`.

What is in it, so each of those meets every case kgfq writes today:

| step | operation | what it shows |
|---|---|---|
| 1 | `graph_summary` | a step with no table: `outputs` is empty and the record holds the only receipt |
| 2 | `count` | an answer in `result` |
| 3 | `fetch` → `chems` | two named variables; literals with a language tag |
| 4 | `fetch` → `affects` | a bound fetch from a file, classified `external` by hash; `chem/99` is asked for and is not in the graph, so two of three values found rows |
| 5 | `fetch` → `pathways` | a second graph, on a second server: its pin names another origin |
| 6 | `fetch` → `chem1` | a constant subject, which has no column and which `inputs` derives from step 3's table; two unnamed positions, whose columns are `p` and `o`; a typed literal and a plain one |
| 7 | `distinct` | an operation only kgfq has, with `top` |

The graphs are `tests/fixtures/chem.nt` and `genes.nt`, served by two `kgf serve` processes
on local ports that no longer exist: the origins in the log are where the session was
fetched, as they always are, and nothing here can be replayed against them.

`tests/test_session_fixture.py` holds both ends. It reads the fixture as above, and it runs
the same seven commands against fresh fixture servers and compares everything that does not
change from run to run. `KGFQ_WRITE_FIXTURE=1 uv run pytest tests/test_session_fixture.py`
rewrites the fixture when the format changes on purpose.
