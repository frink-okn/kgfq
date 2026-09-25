"""``kgfq sparql``'s library half (plan §5.2): launch ``kgf-sparql``, read its
output into a table, wrap its receipt, and apply the two proven-equivalent
normalizations. No planning lives here (§5.5): the engine is a program in
another repository, reached through a subprocess and never linked."""

from __future__ import annotations

from .engine import (
    EXIT_ABORTED,
    EXIT_COMPLETE,
    EXIT_ERROR,
    Budget,
    EngineNotFound,
    EngineRun,
    find_engine,
    parse_bytes,
    run_engine,
)
from .normalize import normalize
from .receipt import requests_by_shape, sparql_receipt
from .results import ResultError, bindings_table, read_output, triples_table

__all__ = [
    "EXIT_ABORTED",
    "EXIT_COMPLETE",
    "EXIT_ERROR",
    "Budget",
    "EngineNotFound",
    "EngineRun",
    "ResultError",
    "bindings_table",
    "find_engine",
    "normalize",
    "parse_bytes",
    "read_output",
    "requests_by_shape",
    "run_engine",
    "sparql_receipt",
    "triples_table",
]
