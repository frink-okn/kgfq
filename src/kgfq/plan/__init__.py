"""The strategy function (plan C4): the one place operation selection lives."""

from .fetch import (  # noqa: F401 — the package's public names
    Plan,
    PlanStep,
    check_guard,
    execute_fetch,
    guard_rows,
    plan_fetch,
)
