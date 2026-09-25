"""Golden-test harness: two real ``kgf serve`` processes over hdtc-built bundles
of two different graphs (plan §4, D14). No mocked server.

Binaries: ``KGFQ_KGF`` and ``KGFQ_HDTC`` name them; otherwise the sibling
checkouts' release builds are used. Tests that need a server skip with the
command to run when neither is available.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
SOURCE = HERE.parent.parent


def _binary(env: str, *candidates: Path) -> Path | None:
    if os.environ.get(env):
        return Path(os.environ[env])
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    found = shutil.which(env.removeprefix("KGFQ_").lower())
    return Path(found) if found else None


KGF = _binary("KGFQ_KGF", SOURCE / "kgf-rs/target/release/kgf", SOURCE / "kgf-rs/target/debug/kgf")
HDTC = _binary("KGFQ_HDTC", SOURCE / "hdtc/target/release/hdtc", SOURCE / "hdtc/target/debug/hdtc")
# kgf-sparql is a Node program: its package entry in the sibling checkout, run by node,
# unless KGFQ_KGF_SPARQL names an executable or entry of its own
KGF_SPARQL_JS = SOURCE / "kgf-sparql/packages/kgf-sparql/bin/kgf-sparql.js"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class Server:
    origin: str
    root: Path
    datasets: dict[str, str]  # id -> version
    process: subprocess.Popen
    log_handle: IO[str] | None = None

    def stop(self) -> None:
        self.process.terminate()
        if self.log_handle:
            self.log_handle.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def build_bundle(root: Path, dataset: str, version: str, source: Path, config: Path) -> None:
    out = root / dataset / version
    result = subprocess.run(
        [
            str(KGF),
            "build",
            "--config",
            str(config),
            "--input",
            str(source),
            "--out",
            str(out),
            "--hdtc",
            str(HDTC),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kgf build failed for {dataset}:\n{result.stderr}")


def spawn_server(root: Path, log: Path, public_base: str | None = None) -> Server:
    handle = open(log, "w")  # noqa: SIM115 — outlives this function; closed by Server.stop()
    process = subprocess.Popen(
        [
            str(KGF),
            "serve",
            "--bundle-root",
            str(root),
            "--bind",
            "127.0.0.1:0",
            "--access-log",
            "off",
            *(["--public-base", public_base] if public_base else []),
        ],
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        text = _ANSI.sub("", log.read_text())
        m = re.search(r"address=(127\.0\.0\.1:\d+)", text)
        if m:
            datasets = {
                d.name: sorted(p.name for p in d.iterdir())[-1]
                for d in root.iterdir()
                if d.is_dir()
            }
            return Server(
                origin=f"http://{m.group(1)}",
                root=root,
                datasets=datasets,
                process=process,
                log_handle=handle,
            )
        if process.poll() is not None:
            raise RuntimeError(f"kgf serve exited: {text}")
        time.sleep(0.05)
    process.kill()
    raise RuntimeError("kgf serve did not report its address in time")


@pytest.fixture(scope="session")
def servers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Server]]:
    """Two origins, one graph each: ``chem`` (chemkg) and ``genes`` (genekg)."""
    if KGF is None or HDTC is None:
        pytest.skip(
            "needs kgf and hdtc binaries: set KGFQ_KGF and KGFQ_HDTC, or build ../kgf-rs and ../hdtc"
        )
    base = tmp_path_factory.mktemp("kgf")
    roots = {"chem": base / "chem-root", "genes": base / "genes-root"}
    build_bundle(
        roots["chem"], "chemkg", "2026-09-01", FIXTURES / "chem.nt", FIXTURES / "chem.build.yaml"
    )
    build_bundle(
        roots["genes"], "genekg", "2026-09-01", FIXTURES / "genes.nt", FIXTURES / "genes.build.yaml"
    )
    started = {name: spawn_server(root, base / f"{name}.log") for name, root in roots.items()}
    yield started
    for s in started.values():
        s.stop()


@pytest.fixture(scope="session")
def kgf_sparql() -> str:
    """The engine ``kgfq sparql`` launches, made findable through its environment
    variable for the session; tests skip when there is none to launch."""
    if not os.environ.get("KGFQ_KGF_SPARQL"):
        if not (KGF_SPARQL_JS.is_file() and shutil.which("node")):
            pytest.skip(
                "needs kgf-sparql: set KGFQ_KGF_SPARQL, or build ../kgf-sparql and have node on PATH"
            )
        os.environ["KGFQ_KGF_SPARQL"] = str(KGF_SPARQL_JS)
    return os.environ["KGFQ_KGF_SPARQL"]


@pytest.fixture(scope="session")
def chem(servers: dict[str, Server]) -> Server:
    return servers["chem"]


@pytest.fixture(scope="session")
def genes(servers: dict[str, Server]) -> Server:
    return servers["genes"]
