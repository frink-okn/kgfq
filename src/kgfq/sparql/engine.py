"""Launching ``kgf-sparql`` (plan §5.2, §5.5). The complete-answering engine is
a separate program in a separate repository, found through ``KGFQ_KGF_SPARQL``
or ``PATH`` and never linked; the process boundary is the contract boundary.
This module hands it sources, a budget and a receipt path, waits, and reads
the receipt back. When the engine is absent the command says how to install
it rather than degrading to anything else."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..client.errors import KgfqError

ENV = "KGFQ_KGF_SPARQL"
EXECUTABLE = "kgf-sparql"
INSTALL_HINT = (
    "kgf-sparql is not installed. Install it with `npm install -g kgf-sparql`, or build the "
    "../kgf-sparql checkout (`npm install && npm run build`), then put `kgf-sparql` on PATH or "
    f"set {ENV} to the executable or to its packages/kgf-sparql/bin/kgf-sparql.js"
)
# How long past its own wall-clock budget the engine may run before kgfq kills it. A
# sleeping machine stops the engine's timers (kgf-sparql handoff 2026-09-10 §2); nothing
# else lets a run outlive its budget.
HARNESS_MARGIN_S = 60.0

# The engine's exit statuses (its README, "Exit status").
EXIT_COMPLETE = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_ABORTED = 3

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)[bB]?\s*$")


class EngineNotFound(KgfqError):
    """No ``kgf-sparql`` to launch; the message says how to get one."""


def find_engine() -> list[str]:
    """The command that runs ``kgf-sparql``: ``KGFQ_KGF_SPARQL`` names an executable
    or the package's JavaScript entry (run by ``node``); otherwise ``kgf-sparql``
    on ``PATH``."""
    named = os.environ.get(ENV)
    if named:
        path = Path(named).expanduser()
        if not path.is_file():
            raise EngineNotFound(f"{ENV}={named} is not a file. {INSTALL_HINT}")
        if path.suffix == ".js":
            node = shutil.which("node")
            if not node:
                raise EngineNotFound(
                    f"{ENV} names {path.name}, which needs node on PATH. {INSTALL_HINT}"
                )
            return [node, str(path)]
        return [str(path)]
    found = shutil.which(EXECUTABLE)
    if found:
        return [found]
    raise EngineNotFound(INSTALL_HINT)


def parse_bytes(text: str | int | None) -> int | None:
    """``256M``, ``1G``, ``4096`` → bytes; ``k``/``M``/``G`` are binary multiples."""
    if text is None:
        return None
    if isinstance(text, int):
        return text
    m = _SIZE.match(text)
    if not m:
        raise KgfqError(f"not a size: {text!r} (use a number, or k/M/G suffixes)")
    scale = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[m.group(2).lower()]
    return int(float(m.group(1)) * scale)


@dataclass(frozen=True, slots=True)
class Budget:
    """The plan-level budget the engine enforces (§5.2 item 2), every part optional
    and unlimited when absent. The defaults are the registry corpus's numbers
    (kgf-sparql handoff 2026-09-10 §2): 20,000 requests and 60 s admit its
    expensive-but-finite queries; 256 MiB of decoded bytes is the guard that stands
    between a plan and the benchmark's heap deaths."""

    max_requests: int | None = 20_000
    max_bytes: int | None = 256 << 20
    max_rows: int | None = None
    timeout_s: float | None = 60.0

    def args(self) -> list[str]:
        out: list[str] = []
        if self.max_requests is not None:
            out += ["--max-requests", str(self.max_requests)]
        if self.max_bytes is not None:
            out += ["--max-bytes", str(self.max_bytes)]
        if self.max_rows is not None:
            out += ["--max-rows", str(self.max_rows)]
        if self.timeout_s is not None:
            out += ["--timeout", str(self.timeout_s)]
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_requests": self.max_requests,
            "max_bytes": self.max_bytes,
            "max_rows": self.max_rows,
            "timeout_s": self.timeout_s,
        }


@dataclass(slots=True)
class EngineRun:
    """One launch: the command line, how it ended, and its receipt when it wrote one."""

    command: list[str]
    exit_code: int | None
    stderr: str
    receipt: dict[str, Any] | None
    output: Path
    receipt_path: Path
    elapsed_ms: float

    @property
    def contract(self) -> str:
        """``complete``, ``aborted`` or ``error`` — the engine's word, or ``error``
        when it left no receipt."""
        if self.receipt:
            return str(self.receipt.get("outcome", {}).get("contract", "error"))
        return "error"

    @property
    def error(self) -> str | None:
        if self.receipt:
            err = self.receipt.get("outcome", {}).get("error")
            if err:
                return str(err)
            if self.contract != "error":
                return None
        tail = self.stderr.strip().splitlines()
        return tail[-1] if tail else f"kgf-sparql exited {self.exit_code} without a receipt"


def run_engine(
    command: list[str],
    query: Path,
    sources: list[str],
    *,
    out: Path,
    receipt: Path,
    budget: Budget,
    http_retries: int = 0,
) -> EngineRun:
    """Run one query to completion, or to a budget, and read the receipt back.
    Output goes to a file, never a pipe (the engine's README): results are what
    the file holds, and the receipt is the account of what that is."""
    argv = [*command, "-f", str(query), "--out", str(out), "--receipt", str(receipt)]
    argv += budget.args()
    if http_retries:
        argv += ["--http-retries", str(http_retries)]
    for source in sources:
        argv += ["--source", source]
    for stale in (out, receipt):
        stale.unlink(missing_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    timeout = None if budget.timeout_s is None else budget.timeout_s + HARNESS_MARGIN_S
    t0 = time.perf_counter()
    exit_code: int | None
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        exit_code, stderr = done.returncode, done.stderr
    except subprocess.TimeoutExpired as e:
        exit_code = None
        captured = e.stderr
        stderr = (
            captured.decode(errors="replace") if isinstance(captured, bytes) else (captured or "")
        )
        stderr += (
            f"\nkgfq: kgf-sparql outlived its {budget.timeout_s} s budget by more than "
            f"{HARNESS_MARGIN_S:.0f} s and was killed"
        )
    elapsed = round((time.perf_counter() - t0) * 1000, 3)
    document = None
    if receipt.exists():
        try:
            document = json.loads(receipt.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            document = None
    return EngineRun(argv, exit_code, stderr, document, out, receipt, elapsed)
