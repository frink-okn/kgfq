"""Run the okn-registry corpus through ``kgfq sparql`` (plan §9, phase A's exit).

Each query is one ``kgfq sparql`` invocation in one workspace: sources from the
header's tags (the legacy ``federation`` tag mapped the way kgf-sparql's own
runner maps it, so the two runs cover the same queries), ``--force`` for the
queries the linter would refuse, the budget of the run it is compared with, and
the result table hashed the way that run hashed its bindings, so the two agree
row for row. The live server is a benchmark subject here, never a test
dependency: this is a script, not a test.

usage: uv run python bench/registry_corpus.py [--compare results.json] [--only substr]...
       [--out bench/registry-corpus-<date>.json] [--normalize]
env:   KGF_ROOT (default https://apps.okn.us/kgf)
       REGISTRY_QUERY_TIMEOUT (seconds, default 30), REGISTRY_MAX_REQUESTS (5000),
       REGISTRY_MAX_BYTES (256M) — the kgf-sparql run's budget, for a like-for-like comparison
       REGISTRY_RUN_DIR (the workspace; default bench/runs/registry-<date>)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
QUERY_ROOT = REPO.parent / "okn-registry" / "docs" / "registry" / "queries"
KGF_SPARQL_RESULTS = REPO.parent / "kgf-sparql-bench" / "test" / "registry-query-results-cli.json"
ROOT = os.environ.get("KGF_ROOT", "https://apps.okn.us/kgf").rstrip("/")
TIMEOUT = os.environ.get("REGISTRY_QUERY_TIMEOUT", "30")
MAX_REQUESTS = os.environ.get("REGISTRY_MAX_REQUESTS", "5000")
MAX_BYTES = os.environ.get("REGISTRY_MAX_BYTES", "256M")
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
MASK_64 = (1 << 64) - 1

# kgf-sparql's runner (test/registry_corpus_common.py): older headers say only
# "federation"; the targets are inferred from their namespaces and kept explicit.
FEDERATION_TARGETS = {
    "fio-spatial-facilities-in-state": ["fiokg", "spatialkg"],
    "nde-diseases-mondo-parents": ["nde", "ubergraph"],
    "sawgraph-hydrology-spatial-04": ["hydrologykg", "spatialkg"],
    "sawgraph-hydrology-spatial_03": ["hydrologykg", "spatialkg"],
    "sawgraph-spatial-hydrology-01": ["hydrologykg", "spatialkg"],
}
ALIASES = {"fio-kg": "fiokg"}
ALL_LOCAL = {"list-things-and-their-labels", "one-hundred-triples"}


def kgfq(workspace: Path, *argv: str) -> tuple[int, str, str]:
    cmd = [sys.executable, "-m", "kgfq.cli", "--workspace", str(workspace), "--json", *argv]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def header_tags(text: str) -> list[str]:
    tags: list[str] = []
    in_tags = False
    for line in text.splitlines():
        if line.startswith("#+ tags:"):
            in_tags = True
            continue
        if in_tags:
            m = re.match(r"^#\+\s+-\s+(.+?)\s*$", line)
            if m:
                tags.append(m.group(1).strip())
                continue
            break
    return tags


def targets_for(path: Path, tags: list[str], available: list[str]) -> tuple[list[str], str | None]:
    lower = path.read_text(encoding="utf-8").lower()
    if "wikidata" in tags or "wikidata.org" in lower or "wikidata" in path.name.lower():
        return [], "wikidata"
    targets = [ALIASES.get(t, t) for t in tags if t != "federation"]
    if targets:
        return targets, None
    if path.stem in FEDERATION_TARGETS:
        return FEDERATION_TARGETS[path.stem], "inferred-federation-targets"
    if path.stem in ALL_LOCAL:
        return available, "all-local-datasets"
    return [], "unresolved-federation-targets"


def determinate(text: str) -> bool:
    """One bag: a LIMIT without ORDER BY leaves the subset to the plan."""
    return not re.search(r"(?i)\bLIMIT\b", text) or bool(re.search(r"(?i)\bORDER\s+BY\b", text))


def term_string(value: str, kind: str | None, lang: str | None, datatype: str | None) -> str:
    """kgf-sparql's runner's rendering of a term, from the table's four columns.
    The runner spells out ``xsd:string``; the table leaves it implicit."""
    if kind != "literal":
        return value if kind == "bnode" else f"<{value}>"
    text = json.dumps(value, ensure_ascii=False)
    return f"{text}@{lang}" if lang else f"{text}^^<{datatype or XSD_STRING}>"


def multiset_hash(table_path: Path) -> tuple[str, list[str]]:
    table = pq.read_table(table_path)
    vars_ = [c for c in table.column_names if not c.endswith(("_kind", "_lang", "_datatype"))]
    columns = {c: table.column(c).to_pylist() for c in table.column_names}
    total = xor = 0
    samples: list[str] = []
    for i in range(table.num_rows):
        parts = []
        for v in vars_:
            value = columns[v][i]
            if value is None:
                continue
            kind, lang, datatype = (columns[f"{v}_{c}"][i] for c in ("kind", "lang", "datatype"))
            parts.append(f"{v}={term_string(value, kind, lang, datatype)}")
        text = "\x01".join(sorted(parts))
        if len(samples) < 3:
            samples.append(text[:500])
        n = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        total = (total + n) & MASK_64
        xor ^= n
    return f"{total:016x}:{xor:016x}", samples


def run_query(
    workspace: Path, path: Path, targets: list[str], out: str, normalize: bool
) -> dict[str, Any]:
    argv = [
        "sparql",
        str(path),
        "--out",
        out,
        "--force",
        "--http-retries",
        "8",
        "--max-requests",
        MAX_REQUESTS,
        "--max-bytes",
        MAX_BYTES,
        "--timeout",
        TIMEOUT,
    ]
    if normalize:
        argv.append("--normalize")
    for t in targets:
        argv += ["--dataset", t]
    started = time.perf_counter()
    code, stdout, stderr = kgfq(workspace, *argv)
    seconds = round(time.perf_counter() - started, 3)
    if not stdout.lstrip().startswith("{"):
        return {
            "ok": False,
            "error": (stderr.strip() or f"exit:{code}")[-500:],
            "exitCode": code,
            "seconds": seconds,
        }
    handle = json.loads(stdout)
    result: dict[str, Any] = {
        "ok": handle["contract"] == "complete",
        "contract": handle["contract"],
        "rows": handle["rows"],
        "requests": handle["requests"],
        "bindingsRequests": handle["bindings_requests"],
        "responseBytes": handle["bytes_decoded"],
        "seconds": round(handle["elapsed_ms"] / 1000, 3),
        "wallSeconds": seconds,
        "exitCode": code,
        "step": handle["step"],
        "lint": handle["lint"],
        "normalizations": [n["rule"] for n in handle["normalizations"] if n["applied"]],
        "table": handle["table"],
    }
    if handle["contract"] == "aborted":
        result["error"] = f"aborted:{handle['budget']}"
        result["budget"] = handle["budget"]
    elif handle["contract"] == "error":
        result["error"] = str(handle["error"])[:500]
    if handle["table"] and handle["columns"] != ["s", "p", "o", "o_kind", "o_lang", "o_datatype"]:
        result["multisetHash"], result["samples"] = multiset_hash(Path(handle["path"]))
    return result


def label(r: dict[str, Any]) -> str:
    if not r.get("ok"):
        return str(r.get("error", "error"))[:24]
    return f"{r.get('rows', 0):>7} rows {r.get('requests', 0):>6} req {r.get('seconds', 0):>5.1f}s"


def compare(results: dict[str, dict[str, Any]], earlier_path: Path) -> dict[str, Any]:
    earlier = json.loads(earlier_path.read_text(encoding="utf-8"))["results"]
    ran = {k: v["result"] for k, v in results.items() if not v["result"].get("skipped")}
    old_nonempty = {
        k for k, v in earlier.items() if v["result"].get("ok") and v["result"].get("rows", 0) > 0
    }
    new_nonempty = {k for k, v in ran.items() if v.get("ok") and v.get("rows", 0) > 0}
    comparable = {k for k in old_nonempty & new_nonempty if results[k].get("determinate")}
    same_hash = {
        k
        for k in comparable
        if earlier[k]["result"].get("multisetHash") == ran[k].get("multisetHash")
    }
    same_rows = {
        k
        for k in old_nonempty & new_nonempty
        if earlier[k]["result"].get("rows") == ran[k].get("rows")
    }
    print(f"\nCompared with {earlier_path.name}:")
    print(
        f"  non-empty results: earlier {len(old_nonempty)}, now {len(new_nonempty)}, "
        f"both {len(old_nonempty & new_nonempty)}, same row count {len(same_rows)}"
    )
    print(f"  identical bags {len(same_hash)} of {len(comparable)} whose query denotes one bag")
    for k in sorted(comparable - same_hash):
        print(
            f"    differing bag: {k}: earlier {label(earlier[k]['result'])} | now {label(ran[k])}"
        )
    for name, keys in (
        ("non-empty earlier, not now", old_nonempty - new_nonempty),
        ("non-empty now, not earlier", new_nonempty - old_nonempty),
    ):
        if keys:
            print(f"  {name} ({len(keys)}):")
            for k in sorted(keys):
                before = earlier.get(k, {}).get("result", {"error": "not in earlier run"})
                print(
                    f"    {k}: earlier {label(before)} | now {label(ran.get(k, {'error': 'not run'}))}"
                )
    aborted_then = {
        k for k, v in earlier.items() if str(v["result"].get("error", "")).startswith("aborted")
    }
    aborted_now = {k for k, v in ran.items() if str(v.get("error", "")).startswith("aborted")}
    print(
        f"  aborted at a budget: earlier {len(aborted_then)}, now {len(aborted_now)}, both {len(aborted_then & aborted_now)}"
    )
    return {
        "earlierNonEmpty": len(old_nonempty),
        "nowNonEmpty": len(new_nonempty),
        "bothNonEmpty": len(old_nonempty & new_nonempty),
        "sameRowCount": len(same_rows),
        "comparable": len(comparable),
        "identicalBags": len(same_hash),
        "lostNonEmpty": sorted(old_nonempty - new_nonempty),
        "gainedNonEmpty": sorted(new_nonempty - old_nonempty),
        "differingBags": sorted(comparable - same_hash),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--compare", type=Path, default=KGF_SPARQL_RESULTS)
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--normalize", action="store_true")
    args = ap.parse_args()
    date = time.strftime("%Y%m%d")
    out = args.out or HERE / f"registry-corpus-{date}.json"
    workspace = Path(os.environ.get("REGISTRY_RUN_DIR", HERE / "runs" / f"registry-{date}"))
    workspace.mkdir(parents=True, exist_ok=True)
    code, stdout, stderr = kgfq(workspace, "endpoints", "add", ROOT)
    if code:
        print(stderr, file=sys.stderr)
        return code
    _, stdout, _ = kgfq(workspace, "datasets")
    available = sorted({d["id"] for d in json.loads(stdout)["datasets"]})
    files = sorted(QUERY_ROOT.rglob("*.rq"))
    if args.only:
        files = [f for f in files if any(s in str(f) for s in args.only)]
    budget = {
        "maxRequests": int(MAX_REQUESTS),
        "maxBytes": MAX_BYTES,
        "timeoutSeconds": float(TIMEOUT),
    }
    print(
        f"{len(files)} registry queries via kgfq sparql; {len(available)} datasets at {ROOT}; budget {budget}; workspace {workspace}",
        flush=True,
    )
    results: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for index, path in enumerate(files, 1):
        query_id = f"{path.parent.name}/{path.stem}"
        text = path.read_text(encoding="utf-8")
        tags = header_tags(text)
        targets, note = targets_for(path, tags, available)
        record: dict[str, Any] = {
            "path": str(path),
            "tags": tags,
            "targets": targets,
            "targetNote": note,
            "determinate": determinate(text),
        }
        unavailable = [t for t in targets if t not in available]
        if note == "wikidata":
            record["result"] = {"ok": False, "skipped": True, "error": "wikidata"}
        elif unavailable:
            record["result"] = {
                "ok": False,
                "skipped": True,
                "error": "unavailable-dataset",
                "datasets": unavailable,
            }
        elif not targets:
            record["result"] = {"ok": False, "skipped": True, "error": note or "no-targets"}
        else:
            table = re.sub(r"[^A-Za-z0-9_]", "_", f"{path.parent.name}__{path.stem}")[:64]
            record["result"] = run_query(workspace, path, targets, table, args.normalize)
        results[query_id] = record
        counts[record["result"].get("error", "ok") if not record["result"].get("ok") else "ok"] += 1
        print(f"[{index:3}/{len(files)}] {query_id:<60} {label(record['result'])}", flush=True)
        out.write_text(
            json.dumps(
                {
                    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "kgfRoot": ROOT,
                    "budget": budget,
                    "normalize": args.normalize,
                    "workspace": str(workspace),
                    "results": results,
                },
                indent=1,
            )
            + "\n"
        )
    print("\nOutcomes:", dict(counts))
    log_lines = sum(
        1 for line in (workspace / "kgfq.log.jsonl").read_text().splitlines() if line.strip()
    )
    ran = sum(1 for r in results.values() if not r["result"].get("skipped"))
    print(
        f"log records: {log_lines} for {ran} runs" + ("" if log_lines == ran else "  <-- MISMATCH")
    )
    summary: dict[str, Any] = {"outcomes": dict(counts), "logRecords": log_lines, "runs": ran}
    if args.compare and args.compare.exists():
        summary["comparison"] = compare(results, args.compare)
    doc = json.loads(out.read_text())
    doc["summary"] = summary
    out.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"results -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
