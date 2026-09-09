"""``kgfq`` — the intent surface as shell commands (plan §6).

JSON on stdout when it is not a TTY, aligned text when it is. Every error from
a server is its problem document, verbatim. Every command that touches the
network or writes a table appends one record to the workspace log (C9).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .. import __version__
from ..client.endpoint import Endpoint, Pattern, Release
from ..client.errors import KgfError, KgfqError, TransportError
from ..client.session import Session
from ..client.statistics import Statistics
from ..client.terms import (
    BadTermSyntax,
    Term,
    Var,
    compact_iri,
    format_term,
    parse_term,
    term_columns,
)
from ..lint import RULES, ParseError, datasets_of, lint, parse_header
from ..plan import execute_fetch, plan_fetch
from ..workspace.log import Step
from ..workspace.receipts import fetch_receipt
from ..workspace.store import Workspace, WorkspaceError

DEFAULT_PREFIXES = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}


class Cli:
    def __init__(self, args: argparse.Namespace, out=None, err=None) -> None:
        self.args = args
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.json = bool(args.json) or not (hasattr(self.out, "isatty") and self.out.isatty())
        self.workspace = Workspace(args.workspace)
        self.session = Session(self.workspace, skin="kgfq", origins_override=args.endpoint or None)

    # -- output ---------------------------------------------------------------

    def emit(
        self,
        payload: Any,
        *,
        table: Sequence[Sequence[Any]] | None = None,
        header: Sequence[str] | None = None,
    ) -> None:
        if self.json or table is None:
            json.dump(payload, self.out, indent=1, ensure_ascii=False)
            self.out.write("\n")
            return
        rows = [list(map(_cell, r)) for r in table]
        if header:
            rows.insert(0, list(header))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))] if rows else []
        for r in rows:
            self.out.write(
                "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() + "\n"
            )

    # -- helpers --------------------------------------------------------------

    def resolve(self, name: str) -> tuple[Endpoint, Release]:
        return self.session.resolve(name, version=self.args.version)

    def prefixes(self, ep: Endpoint, rel: Release) -> dict[str, str]:
        return {**DEFAULT_PREFIXES, **ep.prefixes(rel)}

    def pattern(self, ep: Endpoint, rel: Release, text: str) -> Pattern:
        tokens = _tokenize(text)
        if len(tokens) != 3:
            raise BadTermSyntax(
                f"a pattern is three terms (`?s rdfs:label ?o`), got {len(tokens)}: {text!r}"
            )
        prefixes = self.prefixes(ep, rel)
        parsed = [None if t == "?" else parse_term(t, prefixes) for t in tokens]
        return Pattern(*parsed)

    def values(
        self, ep: Endpoint, rel: Release, path: str | None
    ) -> tuple[list[str], list[list[Term]]] | tuple[None, None]:
        if not path:
            return None, None
        return read_values(Path(path), self.prefixes(ep, rel))

    def stats(self, ep: Endpoint, rel: Release) -> Statistics:
        return Statistics(self.workspace, ep, rel)

    def step(self, command: str, **args: Any) -> Step:
        return Step(
            self.workspace, "cli", command, {k: v for k, v in args.items() if v is not None}
        )

    # -- commands -------------------------------------------------------------

    def endpoints(self) -> int:
        a = self.args
        if a.action == "add":
            current = self.workspace.add_endpoint(a.url)
        elif a.action == "remove":
            current = self.workspace.remove_endpoint(a.url)
        else:
            current = self.workspace.endpoints()
        self.emit({"endpoints": current}, table=[[o] for o in current], header=["origin"])
        return 0

    def datasets(self) -> int:
        entries = self.session.datasets()
        if self.args.text:
            q = self.args.text.lower()
            entries = [
                e
                for e in entries
                if q in (e.id + " " + (e.title or "") + " " + (e.description or "")).lower()
            ]
        payload = {
            "datasets": [dataclasses.asdict(e) for e in entries],
            "unreachable": self.session.unreachable,
        }
        self.emit(
            payload,
            table=[[e.id, e.current, e.triples, e.title, e.origin] for e in entries],
            header=["dataset", "current", "triples", "title", "origin"],
        )
        for origin, why in self.session.unreachable.items():
            self.err.write(f"unreachable: {origin}: {why}\n")
        return 0

    def summary(self) -> int:
        ep, rel = self.resolve(self.args.dataset)
        step = self.step("summary", dataset=self.args.dataset, format=self.args.format)
        step.pin(_pin(ep, rel))
        with ep.trace() as t:
            card = ep.summary(rel, self.args.format)
        step.receipt({"requests": t.requests, "request_ids": t.request_ids})
        step.finish()
        if self.args.format == "md":
            self.out.write(card if card.endswith("\n") else card + "\n")
        else:
            self.emit(card)
        return 0

    def schema(self) -> int:
        a = self.args
        ep, rel = self.resolve(a.dataset)
        prefixes = self.prefixes(ep, rel)
        params: dict[str, Any] = {}
        for key in ("class", "predicate", "datatype", "children", "projection", "view"):
            value = getattr(a, key.replace("-", "_"), None)
            if value:
                params[key] = (
                    format_term(parse_term(value, prefixes))
                    if key in ("class", "predicate", "datatype")
                    else value
                )
        if a.limit:
            params["limit"] = a.limit
        step = self.step("schema", dataset=a.dataset, params=params, all=a.all)
        step.pin(_pin(ep, rel))
        inventory = (
            a.all
            and params.get("children") in ("properties", "classes")
            and not any(
                k in params for k in ("class", "predicate", "datatype", "view", "projection")
            )
        )
        with ep.trace() as t:
            if inventory:
                # the cached inventory (C3): the queryable view, whose numbers
                # equal /count's, fetched once per release
                stats = self.stats(ep, rel)
                body = dict(stats.root())
                body["collection"] = {"kind": params["children"], "order": "published"}
                body["items"] = stats.items(params["children"])
                body["complete"] = True
                body["next"] = None
            elif a.all and ("children" in params or "projection" in params):
                items = list(ep.schema_items(rel, params))
                page = ep.schema_page(rel, {k: v for k, v in params.items() if k != "cursor"})
                body = dict(page.body or {})
                body["items"] = items
                body["complete"] = True
                body["next"] = None
            else:
                body = ep.schema_page(rel, params).body
        step.receipt({"requests": t.requests, "request_ids": t.request_ids})
        step.finish()
        rows = []
        for item in body.get("items", []):
            term = item.get("term") or item.get("predicate") or item.get("class")
            name = compact_iri(term["value"], prefixes) if isinstance(term, dict) else str(term)
            if "class" in item and "predicate" in item:
                name = f"{compact_iri(item['class']['value'], prefixes)} {compact_iri(item['predicate']['value'], prefixes)}"
            counts = item.get("counts") or {k: v for k, v in item.items() if isinstance(v, int)}
            rows.append([item.get("kind", ""), name, json.dumps(counts)])
        self.emit(
            body,
            table=rows or [["node", json.dumps(body.get("node")), ""]],
            header=["kind", "term", "counts"],
        )
        return 0

    def count(self) -> int:
        a = self.args
        ep, rel = self.resolve(a.dataset)
        pattern = self.pattern(ep, rel, a.pattern)
        vars_, rows = self.values(ep, rel, a.values)
        step = self.step("count", dataset=a.dataset, pattern=pattern.as_body(), values=a.values)
        step.pin(_pin(ep, rel))
        step.input_values(_bound_values(pattern))
        if a.values:
            step.input_file(Path(a.values))
        with ep.trace() as t:
            stats = self.stats(ep, rel)
            if rows is None:
                c, source = stats.count_source(pattern)
                payload: dict[str, Any] = {"count": c.value, "exact": c.exact, "source": source}
                if c.min is not None:
                    payload["min"] = c.min
                table = [[c.value, c.exact]]
                header = ["count", "exact"]
            else:
                counts = stats.count_bindings(pattern, vars_ or [], rows)
                payload = {
                    "counts": [
                        {
                            "binding": i,
                            "row": [format_term(v) for v in r],
                            "count": c.value,
                            "exact": c.exact,
                        }
                        for i, (r, c) in enumerate(zip(rows, counts, strict=True))
                    ],
                    "total": sum(c.value for c in counts),
                }
                table = [
                    [i, " ".join(format_term(v) for v in r), c.value]
                    for i, (r, c) in enumerate(zip(rows, counts, strict=True))
                ]
                header = ["binding", "row", "count"]
        step.receipt(
            {"requests": t.requests, "request_ids": t.request_ids, "bytes_wire": t.bytes_wire}
        )
        step.output(**payload if rows is None else {"total": payload["total"]})
        step.finish()
        self.emit(payload, table=table, header=header)
        return 0

    def sample(self) -> int:
        a = self.args
        ep, rel = self.resolve(a.dataset)
        pattern = self.pattern(ep, rel, a.pattern)
        step = self.step("sample", dataset=a.dataset, pattern=pattern.as_body(), n=a.n, seed=a.seed)
        step.pin(_pin(ep, rel))
        with ep.trace() as t:
            rows = ep.sample(rel, pattern, n=a.n, seed=a.seed)
        step.receipt({"requests": t.requests, "request_ids": t.request_ids})
        step.output(rows=len(rows))
        step.finish()
        names = list(pattern.variables().values())
        payload = {
            "vars": names,
            "rows": [{k: format_term(v) for k, v in r.items()} for r in rows],
            "seed": a.seed,
            "advisory": "samples are for eyeballing value shapes; never plan from them",
        }
        self.emit(
            payload,
            table=[[format_term(r[n]) if n in r else "" for n in names] for r in rows],
            header=names,
        )
        return 0

    def explain(self) -> int:
        a = self.args
        ep, rel = self.resolve(a.dataset)
        pattern = self.pattern(ep, rel, a.pattern)
        vars_, rows = self.values(ep, rel, a.values)
        plan = plan_fetch(ep, rel, pattern, vars_=vars_, rows=rows, stats=self.stats(ep, rel))
        self.emit(plan.as_dict())
        return 0

    def fetch(self) -> int:
        a = self.args
        ep, rel = self.resolve(a.dataset)
        pattern = self.pattern(ep, rel, a.pattern)
        vars_, rows = self.values(ep, rel, a.values)
        out = a.out or _default_table_name(pattern)
        step = self.step(
            "fetch",
            dataset=a.dataset,
            pattern=pattern.as_body(),
            values=a.values,
            out=out,
            max_rows=a.max_rows,
        )
        step.pin(_pin(ep, rel))
        inputs = step.input_values(_bound_values(pattern))
        if a.values:
            inputs[str(a.values)] = step.input_file(Path(a.values))
        with ep.trace() as t:
            plan = plan_fetch(ep, rel, pattern, vars_=vars_, rows=rows, stats=self.stats(ep, rel))
            table = execute_fetch(
                ep, rel, pattern, plan, vars_=vars_, rows=rows, max_rows=a.max_rows
            )
            approximations = []
            if a.max_rows is not None and table.num_rows >= a.max_rows:
                approximations.append(
                    f"stopped at --max-rows {a.max_rows}: which rows, not how many, is unspecified (R3)"
                )
            receipt = fetch_receipt(
                step=step.step,
                kind="fetch",
                release=rel,
                intent=plan.intent,
                plan=plan.as_dict(),
                trace=t,
                approximations=approximations,
                inputs=inputs,
            )
        step.plan(plan.as_dict())
        info = write_output(self.workspace, out, table, receipt)
        step.receipt({k: receipt[k] for k in ("totals", "completeness", "approximations", "grade")})
        step.output(
            table=info["table"],
            path=info["path"],
            rows=table.num_rows,
            content_hash=info["content_hash"],
        )
        step.finish()
        handle = {
            "table": info["table"],
            "path": info["path"],
            "rows": table.num_rows,
            "columns": table.column_names,
            "grade": receipt["grade"],
            "requests": receipt["totals"]["requests"],
            "bytes_wire": receipt["totals"]["bytes_wire"],
            "complete": receipt["completeness"]["complete"],
            "approximations": approximations,
            "step": step.step,
            "content_hash": info["content_hash"],
        }
        self.emit(handle, table=[[k, v] for k, v in handle.items()], header=["field", "value"])
        return 0

    def lint(self) -> int:
        """Lint one or more .rq files; exit 1 when a diagnostic at or above
        --fail-on appears, 2 when a file does not parse. Nothing is rewritten."""
        a = self.args
        closed = set(a.closed or [])
        if a.dataset:
            ep, rel = self.resolve(a.dataset)
            descriptor = ep.dataset_descriptor(rel.dataset)
            for key in ("closed_predicates", "transitively_closed", "closed"):
                value = descriptor.get(key)
                if isinstance(value, list):
                    closed |= {str(v) for v in value}
        order = ["info", "warning", "strong"]
        worst = -1
        results = []
        for path in a.files:
            text = Path(path).read_text(encoding="utf-8")
            header = parse_header(text)
            entry: dict[str, Any] = {"file": str(path), "datasets": datasets_of(header)}
            try:
                diagnostics = lint(text, threshold=a.threshold, closed_predicates=closed or None)
            except ParseError as e:
                entry["error"] = str(e)
                worst = max(worst, len(order))
                results.append(entry)
                continue
            entry["diagnostics"] = [d.as_dict() for d in diagnostics]
            for d in diagnostics:
                worst = max(worst, order.index(d.severity))
            results.append(entry)
        if self.json:
            self.emit(
                {
                    "files": results,
                    "rules": {k: {"severity": v[0], "title": v[1]} for k, v in RULES.items()},
                }
            )
        else:
            for entry in results:
                if "error" in entry:
                    self.out.write(f"{entry['file']}: parse error: {entry['error']}\n")
                    continue
                diags = entry["diagnostics"]
                self.out.write(
                    f"{entry['file']}: {len(diags)} diagnostic{'s' if len(diags) != 1 else ''}\n"
                )
                for d in diags:
                    self.out.write(f"  {d['code']} [{d['severity']}] {d['message']}\n")
                    if d.get("suggestion"):
                        self.out.write(f"      → {d['suggestion']}\n")
        if worst >= len(order):
            return 2
        if a.fail_on != "never" and worst >= order.index(a.fail_on):
            return 1
        return 0

    def receipt(self) -> int:
        self.emit(self.workspace.read_receipt(self.args.table))
        return 0

    def log(self) -> int:
        records = self.workspace.read_log()
        if self.args.last:
            records = records[-self.args.last :]
        self.emit(
            {"steps": records},
            table=[
                [r["step"], r["command"], r.get("status", ""), json.dumps(r.get("output", {}))[:80]]
                for r in records
            ],
            header=["step", "command", "status", "output"],
        )
        return 0

    def pins(self) -> int:
        if self.args.clear:
            self.workspace.unpin()
        pins = self.workspace.pins()
        self.emit(
            {k: p.as_dict() for k, p in pins.items()},
            table=[[k, p.version, p.content_digest, p.origin] for k, p in pins.items()],
            header=["dataset", "version", "digest", "origin"],
        )
        return 0


def _bound_values(pattern: Pattern) -> list[str]:
    """The constants a pattern carries, for the derived/asserted classification."""
    return [v for v in (term_columns(t)[0] for t in pattern.bound().values()) if v]


def _pin(ep: Endpoint, rel: Release) -> dict[str, Any]:
    return {
        "origin": ep.origin,
        "dataset": rel.dataset,
        "version": rel.version,
        "content_digest": rel.content_digest,
        "dataset_iri": rel.dataset_iri,
    }


def _cell(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _tokenize(text: str) -> list[str]:
    """Split a pattern into terms, keeping quoted literals and bracketed IRIs whole."""
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            j += 1
            while j < n and not text[j].isspace():
                j += 1
            tokens.append(text[i:j])
            i = j
        elif c == "<":
            j = text.find(">", i)
            j = n if j < 0 else j + 1
            tokens.append(text[i:j])
            i = j
        else:
            j = i
            while j < n and not text[j].isspace():
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _default_table_name(pattern: Pattern) -> str:
    parts = []
    for pos, t in pattern.bound().items():
        value = getattr(t, "value", str(t))
        tail = value.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        parts.append("".join(ch if ch.isalnum() else "_" for ch in tail)[:24] or pos)
    name = "_".join(parts) or "fragment"
    return name if name[0].isalpha() or name[0] == "_" else "t_" + name


def read_values(path: Path, prefixes: dict[str, str]) -> tuple[list[str], list[list[Term]]]:
    """A bindings file: JSON ``{"vars": [...], "rows": [[...]]}`` or a text table
    whose first line names the variables (``?c<TAB>?d``) and whose rows carry
    §3.3 terms, tab-separated."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        doc = json.loads(text)
        vars_ = [v.lstrip("?") for v in doc["vars"]]
        rows = [[_term(t, prefixes) for t in r] for r in doc["rows"]]
        return vars_, rows
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return [], []
    header = [h.strip().lstrip("?") for h in lines[0].split("\t")]
    rows = [[_term(t.strip(), prefixes) for t in line.split("\t")] for line in lines[1:]]
    return header, rows


def _term(text: str, prefixes: dict[str, str]) -> Term:
    t = parse_term(text, prefixes)
    if isinstance(t, Var):
        raise BadTermSyntax(f"a binding value cannot be a variable: {text!r}")
    return t


def write_output(
    ws: Workspace, out: str, table: pa.Table, receipt: dict[str, Any]
) -> dict[str, Any]:
    """``--out name`` writes ``tables/name.parquet`` in the workspace; a path
    ending in .parquet/.csv/.json writes there, with the receipt beside it."""
    if "/" not in out and "." not in out:
        info = ws.write_table(out, table, receipt)
        return {"table": info.name, "path": str(info.path), "content_hash": info.content_hash}
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    content_hash = ws.content_hash(table)
    receipt = {
        **receipt,
        "table": path.stem,
        "rows": table.num_rows,
        "columns": table.column_names,
        "content_hash": content_hash,
    }
    if path.suffix == ".parquet":
        meta = dict(table.schema.metadata or {})
        meta[b"kgfq.receipt"] = json.dumps(receipt, sort_keys=True).encode()
        pq.write_table(table.replace_schema_metadata(meta), path, compression="zstd")
    elif path.suffix == ".csv":
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(table.column_names)
            for row in zip(*(table.column(c).to_pylist() for c in table.column_names), strict=True):
                w.writerow(["" if v is None else v for v in row])
    elif path.suffix == ".json":
        path.write_text(
            json.dumps(
                {"columns": table.column_names, "rows": table.to_pylist()}, ensure_ascii=False
            )
        )
    else:
        raise WorkspaceError(
            f"--out must be a table name or a .parquet/.csv/.json path, got {out!r}"
        )
    path.with_name(path.stem + ".receipt.json").write_text(
        json.dumps(receipt, indent=1, sort_keys=True) + "\n"
    )
    return {"table": path.stem, "path": str(path), "content_hash": content_hash}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kgfq", description="Query Knowledge Graph Fragments with a plan and a receipt."
    )
    p.add_argument("--version", action="version", version=f"kgfq {__version__}")
    p.add_argument(
        "--workspace",
        help="workspace directory (default: $KGFQ_WORKSPACE or the current directory)",
    )
    p.add_argument("--json", action="store_true", help="JSON output even on a TTY")
    p.add_argument(
        "--endpoint",
        action="append",
        help="use this origin instead of the workspace's endpoints (repeatable)",
    )
    p.add_argument(
        "--release", dest="version", help="a specific release instead of the pinned/current one"
    )
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("endpoints", help="the workspace's graph endpoints")
    e.add_argument("action", choices=["add", "list", "remove"], nargs="?", default="list")
    e.add_argument("url", nargs="?")

    d = sub.add_parser("datasets", help="every graph on every endpoint")
    d.add_argument("--text", help="substring filter over id, title and description")

    s = sub.add_parser("summary", help="the summary card")
    s.add_argument("dataset")
    s.add_argument("--format", choices=["md", "json"], default="md")

    sc = sub.add_parser("schema", help="drill one bounded edge of the schema")
    sc.add_argument("dataset")
    sc.add_argument("--class", dest="class_")
    sc.add_argument("--predicate")
    sc.add_argument("--datatype")
    sc.add_argument(
        "--children", choices=["classes", "properties", "object-classes", "datatypes", "languages"]
    )
    sc.add_argument("--projection", choices=["class-properties", "class-relations"])
    sc.add_argument("--view", choices=["design", "queryable"])
    sc.add_argument("--limit", type=int)
    sc.add_argument("--all", action="store_true", help="follow cursors to the end")

    for name, help_ in (
        ("count", "count a pattern, or each row of --values"),
        ("explain", "print the plan for a fetch without executing it"),
        ("fetch", "fetch a pattern into a table with a receipt"),
    ):
        c = sub.add_parser(name, help=help_)
        c.add_argument("dataset")
        c.add_argument("pattern", help="three terms: `?s rdfs:label ?o`")
        c.add_argument("--values", help="bindings file (JSON or tab-separated with a ?var header)")
        if name == "fetch":
            c.add_argument("--out", help="table name, or a .parquet/.csv/.json path")
            c.add_argument("--max-rows", type=int)

    sm = sub.add_parser("sample", help="a seeded sample of a pattern")
    sm.add_argument("dataset")
    sm.add_argument("pattern")
    sm.add_argument("-n", type=int, default=10)
    sm.add_argument("--seed", type=int, default=0)

    ln = sub.add_parser("lint", help="lint SPARQL files (KGFQ001–KGFQ012); never rewrites")
    ln.add_argument("files", nargs="+")
    ln.add_argument(
        "--threshold", type=int, default=6, help="complex-pattern review threshold (KGFQ008)"
    )
    ln.add_argument(
        "--closed",
        action="append",
        help="a predicate IRI the dataset publishes transitively closed (KGFQ012); repeatable",
    )
    ln.add_argument(
        "--dataset",
        help="read closed predicates from this dataset's descriptor when it declares them",
    )
    ln.add_argument(
        "--fail-on",
        choices=["strong", "warning", "info", "never"],
        default="strong",
        help="exit 1 at or above this severity",
    )

    r = sub.add_parser("receipt", help="the receipt attached to a table")
    r.add_argument("table")
    lg = sub.add_parser("log", help="the session log")
    lg.add_argument("--last", type=int)
    pi = sub.add_parser("pins", help="the pinned releases")
    pi.add_argument("--clear", action="store_true")
    return p


def main(argv: Sequence[str] | None = None, out=None, err=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "schema":
        args.class_ = getattr(args, "class_", None)
        setattr(args, "class", args.class_)
    cli = Cli(args, out=out, err=err)
    try:
        return getattr(cli, args.command)()
    except KgfError as e:
        cli.err.write(json.dumps(e.problem, indent=1) + "\n")
        return 2
    except (KgfqError, BadTermSyntax, WorkspaceError, TransportError, FileNotFoundError) as e:
        cli.err.write(f"kgfq: {e}\n")
        return 2
    finally:
        cli.session.close()


if __name__ == "__main__":
    sys.exit(main())
