"""``kgfq`` — the intent surface as shell commands (plan §6).

JSON on stdout when it is not a TTY, aligned text when it is. Every error from
a server is its problem document, verbatim. Every command that touches the
network or writes a table appends one record to the workspace log (C9).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pyarrow as pa

from .. import __version__, ops
from ..client.endpoint import Endpoint, Pattern, Release
from ..client.errors import KgfError, KgfqError, TransportError
from ..client.session import Session
from ..client.statistics import Statistics
from ..client.terms import (
    BadTermSyntax,
    Iri,
    Term,
    Var,
    compact_iri,
    format_term,
    parse_term,
    term_columns,
)
from ..lint import RULES, Diagnostic, ParseError, datasets_of, lint, parse_header
from ..plan import check_guard, execute_fetch, guard_rows, plan_fetch
from ..sparql import (
    Budget,
    ResultError,
    find_engine,
    normalize,
    parse_bytes,
    read_output,
    run_engine,
    sparql_receipt,
)
from ..workspace.bind import bind_rows, parse_bind, tuples_in, unmatched_report
from ..workspace.log import Step
from ..workspace.receipts import completeness, fetch_receipt, request_receipt
from ..workspace.store import Workspace, WorkspaceError, stamped, write_parquet
from ..workspace.triples import triples, write_ntriples

# A command goes by the name the session format's registry gives its operation
# (docs/session/operations.yaml), so the word typed, the word logged and a tool's
# name are one. The names kgfq had first, and the hyphenated spellings, still run.
ALIASES = {
    "datasets": "list_graphs",
    "list-graphs": "list_graphs",
    "summary": "graph_summary",
    "graph-summary": "graph_summary",
}

BIND_HELP = (
    "bind pattern variables to an earlier table's columns: `chems:c=chem` gives ?c the "
    "distinct values of table chems's column chem"
)

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
        self.ctx = ops.Context(self.workspace, self.session, surface="cli", version=args.version)

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

    def bindings(
        self, ep: Endpoint, rel: Release
    ) -> tuple[list[str] | None, list[list[Term]] | None, dict[str, Any] | None]:
        """What gives the pattern its bindings, if anything does: ``--bind``, a
        column of an earlier table, or ``--values``, a file. Returns the
        variables, the rows, and the registry's ``bind`` when it was a table."""
        values, bind = getattr(self.args, "values", None), getattr(self.args, "bind", None)
        if values and bind:
            raise KgfqError("--bind and --values both give the pattern its bindings; pass one")
        if bind:
            spec = parse_bind(bind)
            vars_, rows = bind_rows(self.workspace, spec)
            return vars_, rows, spec
        if values:
            vars_, rows = read_values(Path(values), self.prefixes(ep, rel))
            return vars_, rows, None
        return None, None, None

    def bound_from(self, step: Step, spec: dict[str, Any] | None) -> dict[str, Any]:
        """Classify what the bindings came from in the step's inputs: an
        earlier table is derived, a file is external by its hash (C9)."""
        if spec:
            return {spec["table"]: step.input_table(spec["table"])}
        if getattr(self.args, "values", None):
            return {str(self.args.values): step.input_file(Path(self.args.values))}
        return {}

    def stats(self, ep: Endpoint, rel: Release) -> Statistics:
        return Statistics(self.workspace, ep, rel)

    def step(self, operation: str, **args: Any) -> Step:
        """One log record, named and argued as the registry spells the operation
        (``docs/session/operations.yaml``), not as the command line took it."""
        return Step(
            self.workspace, "cli", operation, {k: v for k, v in args.items() if v is not None}
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

    def list_graphs(self) -> int:
        """``kgfq list_graphs``: every graph on every endpoint of the workspace,
        never grouped by server (D14). A step like any other: which graphs
        there were to choose from is part of what happened. It reads each
        origin's service descriptor and no release, so it pins nothing."""
        origins = self.session.origins()
        step = self.step("list_graphs", text=self.args.text)
        with ExitStack() as stack:
            traces = [stack.enter_context(self.session.endpoint(o).trace()) for o in origins]
            entries = self.session.datasets()
        listed = len(entries)
        step.receipt(
            request_receipt(
                step=step.step,
                kind="list_graphs",
                sources=[{"kind": "origin", "grade": "bundle", "origin": o} for o in origins],
                records=[r for t in traces for r in t.records],
            )
        )
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
        step.result(graphs=listed, shown=len(entries), unreachable=dict(self.session.unreachable))
        step.finish()
        self.emit(
            payload,
            table=[[e.id, e.current, e.triples, e.title, e.origin] for e in entries],
            header=["dataset", "current", "triples", "title", "origin"],
        )
        for origin, why in self.session.unreachable.items():
            self.err.write(f"unreachable: {origin}: {why}\n")
        return 0

    def graph_summary(self) -> int:
        ep, rel = self.resolve(self.args.dataset)
        step = self.step("graph_summary", graph=self.args.dataset, format=self.args.format)
        step.pin(_pin(ep, rel))
        with ep.trace() as t:
            card = ep.summary(rel, self.args.format)
        step.receipt(
            request_receipt(step=step.step, kind="graph_summary", release=rel, records=t.records)
        )
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
        logged: dict[str, Any] = {}
        for key in ("class", "predicate", "datatype", "children", "projection", "view"):
            value = getattr(a, key.replace("-", "_"), None)
            if not value:
                continue
            if key in ("class", "predicate", "datatype"):
                term = parse_term(value, prefixes)
                params[key] = format_term(term)
                # the registry's schema arguments are IRIs, absolute and bare
                logged[key] = term.value if isinstance(term, Iri) else format_term(term)
            else:
                params[key] = logged[key] = value
        if a.limit:
            params["limit"] = logged["limit"] = a.limit
        step = self.step("schema", graph=a.dataset, **logged, all=a.all or None)
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
        step.receipt(request_receipt(step=step.step, kind="schema", release=rel, records=t.records))
        step.result(items=len(body.get("items", [])), complete=body.get("complete"))
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
        vars_, rows, bind = self.bindings(ep, rel)
        step = self.step(
            "count", graphs=[a.dataset], **_positions(pattern), bind=bind, values=a.values
        )
        step.pin(_pin(ep, rel))
        step.input_values(_bound_values(pattern))
        self.bound_from(step, bind)
        report: dict[str, Any] | None = None
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
                report = unmatched_report(
                    vars_ or [],
                    rows,
                    {
                        tuple(format_term(v) for v in r)
                        for r, c in zip(rows, counts, strict=True)
                        if c.value
                    },
                    complete=True,
                    bind=bind,
                    values=a.values,
                )
                payload["bound"] = report
                table = [
                    [i, " ".join(format_term(v) for v in r), c.value]
                    for i, (r, c) in enumerate(zip(rows, counts, strict=True))
                ]
                header = ["binding", "row", "count"]
        step.receipt(
            request_receipt(
                step=step.step, kind="count", release=rel, records=t.records, bindings=report
            )
        )
        step.result(**payload if rows is None else {"total": payload["total"], "bound": report})
        step.finish()
        self.emit(payload, table=table, header=header)
        return 0

    def sample(self) -> int:
        a = self.args
        ep, rel = self.resolve(a.dataset)
        pattern = self.pattern(ep, rel, a.pattern)
        step = self.step("sample", graph=a.dataset, **_positions(pattern), n=a.n, seed=a.seed)
        step.pin(_pin(ep, rel))
        with ep.trace() as t:
            rows = ep.sample(rel, pattern, n=a.n, seed=a.seed)
        step.receipt(request_receipt(step=step.step, kind="sample", release=rel, records=t.records))
        step.result(rows=len(rows))
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

    def search(self) -> int:
        """``kgfq search``: the hits, and the table they are filed as."""
        a = self.args
        handle = ops.search(self.ctx, a.dataset, a.text, limit=a.limit, out=a.out)
        self.emit(
            handle,
            table=[
                [h["label"] or "", h["subject"], h["matched"] or "", h["match_kind"] or ""]
                for h in handle["hits"]
            ]
            + [["", f"→ {handle['rows']} hit(s), filed as table {handle['table']}", "", ""]],
            header=["label", "subject", "matched", "kind"],
        )
        return 0

    def distinct(self) -> int:
        """``kgfq distinct`` (doc 03 §3.4.13): the distinct values of one
        position of a pattern, each with its triples — and under ``--values``
        the input rows that reached it. Never reads the triples behind them."""
        a = self.args
        ep, rel = self.resolve(a.dataset)
        pattern = self.pattern(ep, rel, a.pattern)
        vars_, rows, bind = self.bindings(ep, rel)
        step = self.step(
            "distinct",
            graph=a.dataset,
            **_positions(pattern),
            by=a.by,
            bind=bind,
            values=a.values,
            limit=a.limit,
            top=a.top,
        )
        step.pin(_pin(ep, rel))
        step.input_values(_bound_values(pattern))
        self.bound_from(step, bind)
        with ep.trace() as t:
            answer = ep.distinct(rel, pattern, a.by, vars_=vars_, rows=rows, limit=a.limit)
        prefixes = self.prefixes(ep, rel)
        values = answer.values
        if a.top is not None:
            values = sorted(values, key=lambda v: v.count, reverse=True)[: a.top]
        payload: dict[str, Any] = {
            "group_by": answer.group_by,
            "distinct": len(answer.values),
            "triples": answer.triples,
            "rows": [
                {
                    "value": format_term(v.term),
                    "count": v.count,
                    **({} if v.bindings is None else {"bindings": v.bindings}),
                }
                for v in values
            ],
        }
        if a.top is not None:
            payload["top"] = a.top
        step.receipt(
            request_receipt(step=step.step, kind="distinct", release=rel, records=t.records)
        )
        step.result(distinct=len(answer.values), triples=answer.triples)
        step.finish()
        with_bindings = rows is not None
        self.emit(
            payload,
            table=[
                [_short(v.term, prefixes), v.count, *([v.bindings] if with_bindings else [])]
                for v in values
            ],
            header=["value", "count", *(["bindings"] if with_bindings else [])],
        )
        return 0

    def explain(self) -> int:
        a = self.args
        ep, rel = self.resolve(a.dataset)
        pattern = self.pattern(ep, rel, a.pattern)
        vars_, rows, _ = self.bindings(ep, rel)
        plan = plan_fetch(
            ep, rel, pattern, vars_=vars_, rows=rows, stats=self.stats(ep, rel), guard=guard_rows()
        )
        self.emit(plan.as_dict())
        return 0

    def fetch(self) -> int:
        a = self.args
        ep, rel = self.resolve(a.dataset)
        pattern = self.pattern(ep, rel, a.pattern)
        vars_, rows, bind = self.bindings(ep, rel)
        out = a.out or _default_table_name(pattern)
        step = self.step(
            "fetch",
            graph=a.dataset,
            **_positions(pattern),
            bind=bind,
            values=a.values,
            max_rows=a.max_rows,
            all=a.all or None,
            out=out,
        )
        step.pin(_pin(ep, rel))
        inputs = step.input_values(_bound_values(pattern))
        inputs.update(self.bound_from(step, bind))
        with ep.trace() as t:
            plan = plan_fetch(
                ep,
                rel,
                pattern,
                vars_=vars_,
                rows=rows,
                stats=self.stats(ep, rel),
                guard=guard_rows(),
            )
            check_guard(plan, pattern, rel, max_rows=a.max_rows, allow_all=a.all)
            table = execute_fetch(
                ep, rel, pattern, plan, vars_=vars_, rows=rows, max_rows=a.max_rows
            )
            approximations = []
            if any(r.stopped == "max_rows" for r in t.records):
                approximations.append(
                    f"stopped at --max-rows {a.max_rows}: which rows, not how many, is unspecified (R3)"
                )
            report = None
            if rows is not None:
                report = unmatched_report(
                    vars_ or [],
                    rows,
                    tuples_in(table, vars_ or []),
                    complete=completeness(t.records)["complete"],
                    bind=bind,
                    values=a.values,
                )
            receipt = fetch_receipt(
                step=step.step,
                kind="fetch",
                release=rel,
                intent=plan.intent,
                plan=plan.as_dict(),
                trace=t,
                pattern=pattern.as_body(),
                approximations=approximations,
                inputs=inputs,
                bindings=report,
            )
        info = write_output(self.workspace, out, table, receipt)
        core = info["receipt"]["core"]
        step.receipt(info["receipt"])
        step.table(info["receipt"], path=None if info["in_workspace"] else info["path"])
        step.finish()
        handle = {
            "table": info["table"],
            "path": info["path"],
            "rows": table.num_rows,
            "columns": table.column_names,
            "grade": core["grade"],
            "requests": core["totals"]["requests"],
            "bytes_wire": core["totals"]["bytes_wire"],
            "complete": core["completeness"]["complete"],
            "approximations": approximations,
            "step": step.step,
            "content_hash": info["content_hash"],
        }
        if report is not None:
            # where a silent loss becomes a finding: say it where it changes
            # what is done next, not only in the receipt (C6)
            handle["bound"] = report
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

    def sparql(self) -> int:
        """Run one SPARQL file to completion through ``kgf-sparql`` (plan §5.2):
        lint; select sources from ``--dataset`` or the ``#+ tags`` header, each
        pinned; launch with a budget and a receipt path; file the receipt with
        the diagnostics and any normalization attached. Exit 0 complete, 3
        aborted at a budget (receipt written), 1 the engine reported an error
        (its message verbatim), 2 a refusal of kgfq's own."""
        a = self.args
        command = find_engine()  # refuse before any work when there is nothing to launch
        path = Path(a.file)
        text = path.read_text(encoding="utf-8")
        datasets = list(a.dataset or []) or datasets_of(parse_header(text))
        endpoints = list(a.sparql_endpoint or [])
        if not datasets and not endpoints:
            raise KgfqError(
                f"{path}: no sources — give --dataset (repeatable), --sparql-endpoint, or a `#+ tags:` header naming the datasets"
            )
        closed = set(a.closed or []) or None
        budget = Budget(
            max_requests=a.max_requests or None,
            max_bytes=parse_bytes(a.max_bytes) or None,
            max_rows=a.max_rows,
            timeout_s=a.timeout or None,
        )
        out = a.out or _table_name_for(path)
        self.workspace.check_name(out)
        step = self.step(
            "sparql",
            file=str(path),
            graphs=datasets or None,
            sparql_endpoints=endpoints or None,
            out=out,
            budget=budget.as_dict(),
            http_retries=a.http_retries or None,
            normalize=a.normalize or None,
            threshold=a.threshold,
            force=a.force or None,
        )
        inputs = {str(path): step.input_file(path)}
        try:
            diagnostics = lint(text, threshold=a.threshold, closed_predicates=closed)
        except ParseError as e:
            step.finish("error", f"parse error: {e}")
            raise KgfqError(f"{path}: parse error: {e}") from None
        query, normalizations = text, []
        if a.normalize:
            query, normalizations = normalize(
                text, diagnostics, threshold=a.threshold, closed_predicates=closed
            )
            if query != text:
                diagnostics = lint(query, threshold=a.threshold, closed_predicates=closed)
        lint_report = {"threshold": a.threshold, "diagnostics": [d.as_dict() for d in diagnostics]}
        refusing = sorted(
            {d.code for d in diagnostics if d.severity == "strong" or d.code == "KGFQ008"}
        )
        if refusing and not a.force:
            self._write_diagnostics(self.err, str(path), diagnostics)
            reason = (
                f"refused: {', '.join(refusing)}; decompose it, or pass --force to run it anyway"
            )
            step.finish("error", reason)
            raise KgfqError(f"{path}: {reason}")
        sources: list[dict[str, Any]] = []
        specs: list[str] = []
        for name in datasets:
            ep, rel = self.resolve(name)
            step.pin(_pin(ep, rel))
            url = ep.link_url(rel, "fragment")
            sources.append(
                {
                    "kind": "kgf",
                    "grade": "bundle",
                    "origin": ep.origin,
                    "dataset": rel.dataset,
                    "dataset_iri": rel.dataset_iri,
                    "version": rel.version,
                    "content_digest": rel.content_digest,
                    "url": url,
                }
            )
            specs.append(f"kgf@{url}")
        for url in endpoints:
            sources.append({"kind": "sparql", "grade": "sparql", "url": url})
            specs.append(f"sparql@{url}")
        scratch = self.workspace.scratch_dir()
        query_path = scratch / f"sparql-{step.step}.rq"
        query_path.write_text(query, encoding="utf-8")
        output = scratch / f"sparql-{step.step}.out"
        engine_receipt = self.workspace.engine_receipt_path(out)
        run = run_engine(
            command,
            query_path,
            specs,
            out=output,
            receipt=engine_receipt,
            budget=budget,
            http_retries=a.http_retries,
        )
        pointer = None
        if engine_receipt.exists():
            pointer = {
                "path": self.workspace.relative(engine_receipt),
                "sha256": _sha256(engine_receipt),
                "schema": (run.receipt or {}).get("schema"),
            }
        contract = run.contract
        budget_name = (run.receipt or {}).get("outcome", {}).get("budget")
        approximations: list[str] = []
        table: pa.Table | None = None
        shape: str | None = None
        boolean: bool | None = None
        read_error: str | None = None
        if contract == "complete" or (contract == "aborted" and budget_name == "max_rows"):
            try:
                result_type, result = read_output(run)
            except ResultError as e:
                read_error = str(e)
            else:
                if isinstance(result, bool):
                    boolean = result
                else:
                    table = result
                    shape = "triples" if result_type == "quads" else "bindings"
            if contract == "aborted" and read_error is None:
                approximations.append(
                    f"stopped at --max-rows {budget.max_rows}: which rows, not how many, is unspecified"
                )
        receipt = sparql_receipt(
            step=step.step,
            sources=sources,
            query=query,
            original_query=text if query != text else None,
            normalizations=normalizations,
            lint=lint_report,
            run=run,
            budget=budget,
            engine_receipt=pointer,
            inputs=inputs,
            approximations=approximations,
            shape=shape,
        )
        core, detail = receipt["core"], receipt["detail"]
        if read_error:
            contract = "error"
            detail["outcome"] = {**detail["outcome"], "contract": "error", "error": read_error}
            core["completeness"].update(complete=False, incomplete_because=["error"])
        error = detail["outcome"].get("error") if contract == "error" else None
        if table is not None:
            info = self.workspace.write_table(out, table, receipt)
            receipt = info.receipt
            core = receipt["core"]
            step.table(receipt)
            step.result(contract=contract)
        else:
            replaced = self.workspace.remove_table(out)
            self.workspace.write_receipt(out, receipt)
            produced: dict[str, Any] = {
                "contract": contract,
                "rows": detail["outcome"].get("rows", 0),
            }
            if boolean is not None:
                produced["boolean"] = boolean
            if budget_name:
                produced["budget"] = budget_name
            if replaced:
                produced["replaced_table"] = out
            step.result(**produced)
        step.receipt(receipt)
        step.finish("error" if contract == "error" else "ok", error)
        for stale in (output, query_path):
            stale.unlink(missing_ok=True)
        handle: dict[str, Any] = {
            "contract": contract,
            "table": out if table is not None else None,
            "path": str(self.workspace.table_path(out)) if table is not None else None,
            "rows": table.num_rows if table is not None else detail["outcome"].get("rows", 0),
            "columns": table.column_names if table is not None else None,
            "boolean": boolean,
            "grade": core["grade"],
            "budget": budget_name,
            "requests": core["totals"]["requests"],
            "bindings_requests": core["totals"]["bindings_requests"],
            "bytes_decoded": core["totals"]["bytes_decoded"],
            "elapsed_ms": core["totals"]["elapsed_ms"],
            "lint": [d.code for d in diagnostics],
            "normalizations": normalizations,
            "approximations": approximations,
            "engine_receipt": pointer["path"] if pointer else None,
            "receipt": str(self.workspace.receipt_path(out)),
            "step": step.step,
            "content_hash": core.get("content_hash"),
            "error": error,
        }
        if not self.json and diagnostics:
            self._write_diagnostics(self.out, str(path), diagnostics)
        self.emit(
            handle,
            table=[[k, v] for k, v in handle.items() if v is not None],
            header=["field", "value"],
        )
        if error:
            self.err.write(error.rstrip("\n") + "\n")
        return {"complete": 0, "aborted": 3}.get(contract, 1)

    def _write_diagnostics(self, stream: Any, name: str, diagnostics: Sequence[Diagnostic]) -> None:
        stream.write(
            f"{name}: {len(diagnostics)} diagnostic{'s' if len(diagnostics) != 1 else ''}\n"
        )
        for d in diagnostics:
            stream.write(f"  {d.code} [{d.severity}] {d.message}\n")
            if d.suggestion:
                stream.write(f"      → {d.suggestion}\n")

    def export(self) -> int:
        """``kgfq export <table>``: the triples a table stands for, as
        N-Triples, to stdout or ``--out``. A fetched table's rows go back into
        its receipt's pattern (C6). It renders a table the workspace already
        holds, so it makes no request and is not a step."""
        a = self.args
        result = triples(self.workspace.read_table(a.table), self.workspace.read_receipt(a.table))
        if not a.out:
            write_ntriples(result, self.out)
            return 0
        path = Path(a.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            written = write_ntriples(result, f)
        handle = {"table": a.table, "format": a.format, "triples": written, "path": str(path)}
        self.emit(handle, table=[[k, v] for k, v in handle.items()], header=["field", "value"])
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
                [
                    r["step"],
                    r["operation"],
                    r.get("status", ""),
                    ", ".join(o["table"] for o in r.get("outputs", []))
                    or json.dumps(r.get("result", {}))[:80],
                ]
                for r in records
            ],
            header=["step", "operation", "status", "wrote"],
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


def _positions(pattern: Pattern) -> dict[str, str]:
    """A pattern as the registry spells it in ``args``: each position a term in
    doc 03 §3.3 syntax or its ``?variable``, and an unnamed one left out."""
    return {
        key: str(v) if isinstance(v, Var) else format_term(v)
        for key, v in (("subject", pattern.s), ("predicate", pattern.p), ("object", pattern.o))
        if v is not None
    }


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


def _short(term: Term, prefixes: dict[str, str]) -> str:
    """A term for a terminal table: an IRI compacted where a prefix fits."""
    if isinstance(term, Iri):
        return compact_iri(term.value, prefixes)
    return format_term(term)


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


def _table_name_for(path: Path) -> str:
    """A query file's stem as a table name."""
    name = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in path.stem)[:64]
    return name if name and (name[0].isalpha() or name[0] == "_") else "q_" + name


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
    ws: Workspace, out: str, table: pa.Table, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    """``--out name`` writes ``tables/name.parquet`` in the workspace; a path
    ending in .parquet/.csv/.json writes there, with the receipt beside it."""
    if "/" not in out and "." not in out:
        info = ws.write_table(out, table, receipt)
        return {
            "table": info.name,
            "path": str(info.path),
            "content_hash": info.content_hash,
            "receipt": info.receipt,
            "in_workspace": True,
        }
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    content_hash = ws.content_hash(table)
    receipt = stamped(
        receipt,
        table=path.stem,
        rows=table.num_rows,
        columns=table.column_names,
        content_hash=content_hash,
    )
    if path.suffix == ".parquet":
        write_parquet(path, table, receipt, name=path.stem)
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
    return {
        "table": path.stem,
        "path": str(path),
        "content_hash": content_hash,
        "receipt": receipt,
        "in_workspace": False,
    }


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

    d = sub.add_parser(
        "list_graphs", aliases=["list-graphs", "datasets"], help="every graph on every endpoint"
    )
    d.add_argument("--text", help="substring filter over id, title and description")

    s = sub.add_parser(
        "graph_summary", aliases=["graph-summary", "summary"], help="a graph's summary card"
    )
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
        c.add_argument("--bind", help=BIND_HELP)
        if name == "fetch":
            c.add_argument("--out", help="table name, or a .parquet/.csv/.json path")
            c.add_argument("--max-rows", type=int)
            c.add_argument(
                "--all",
                action="store_true",
                help="fetch a pattern larger than the guard (1,000,000 rows) whole",
            )

    fc = sub.add_parser(
        "distinct", help="the distinct values of one position of a pattern, with counts"
    )
    fc.add_argument("dataset")
    fc.add_argument("pattern", help="three terms: `? fbi:OffenseUCRCode ?code`")
    fc.add_argument("--by", required=True, choices=["s", "p", "o"], help="the grouped position")
    fc.add_argument("--values", help="bindings file (JSON or tab-separated with a ?var header)")
    fc.add_argument("--bind", help=BIND_HELP)
    fc.add_argument("--limit", type=int, help="groups per page (default: the server's max_limit)")
    fc.add_argument(
        "--top", type=int, help="print only the N largest values (all values are still read)"
    )

    se = sub.add_parser("search", help="find things by name: full-text search over one graph")
    se.add_argument("dataset")
    se.add_argument("text", help="words to find")
    se.add_argument("--limit", type=int, default=ops.SEARCH_LIMIT, help="most hits to keep")
    se.add_argument("--out", help="table name (default: search_<the words>)")

    sm = sub.add_parser("sample", help="a seeded sample of a pattern")
    sm.add_argument("dataset")
    sm.add_argument("pattern")
    sm.add_argument("-n", type=int, default=10)
    sm.add_argument("--seed", type=int, default=0)

    ln = sub.add_parser("lint", help="lint SPARQL files (KGFQ001-KGFQ012); never rewrites")
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

    sp = sub.add_parser(
        "sparql",
        help="run a SPARQL file to completion through kgf-sparql, with a budget and a receipt",
    )
    sp.add_argument("file", help="the query; its `#+ tags:` header names the datasets by default")
    sp.add_argument("--dataset", action="append", help="a source graph, pinned (repeatable)")
    sp.add_argument(
        "--sparql-endpoint",
        action="append",
        help="a SPARQL endpoint named explicitly, graded `sparql` (repeatable)",
    )
    sp.add_argument("--out", help="table name (default: the file's stem)")
    default = Budget()
    sp.add_argument(
        "--max-requests",
        type=int,
        default=default.max_requests,
        help=f"abort after this many requests (default {default.max_requests}; 0 for no limit)",
    )
    sp.add_argument(
        "--max-bytes",
        default="256M",
        help="abort after this many decoded bytes (default 256M; 0 for no limit)",
    )
    sp.add_argument(
        "--max-rows",
        type=int,
        help="stop after this many result rows, recorded as an approximation",
    )
    sp.add_argument(
        "--timeout",
        type=float,
        default=default.timeout_s,
        help="wall-clock seconds (default 60; 0 for no limit)",
    )
    sp.add_argument(
        "--http-retries", type=int, default=0, help="retry 429/502/503/504 this many times"
    )
    sp.add_argument(
        "--normalize",
        action="store_true",
        help="apply the proven-equivalent KGFQ001/KGFQ002 rewrites, each recorded in the receipt",
    )
    sp.add_argument(
        "--threshold", type=int, default=6, help="complex-pattern review threshold (KGFQ008)"
    )
    sp.add_argument(
        "--closed", action="append", help="a predicate the dataset publishes closed (KGFQ012)"
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="run despite a strong diagnostic or the review threshold",
    )

    ex = sub.add_parser("export", help="a table's triples as N-Triples")
    ex.add_argument("table")
    ex.add_argument("--format", choices=["nt"], default="nt")
    ex.add_argument("--out", help="a file to write (default: stdout)")
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
        return getattr(cli, ALIASES.get(args.command, args.command))()
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
