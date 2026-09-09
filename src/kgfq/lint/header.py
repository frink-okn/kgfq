"""The registry corpus's ``#+`` header convention (plan §5.2): a YAML document
in comment lines before the query — ``summary``, ``tags`` (the datasets the
query is about), and whatever else an author adds."""

from __future__ import annotations

from typing import Any

import yaml


def parse_header(text: str) -> dict[str, Any]:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#+"):
            lines.append(stripped[2:].removeprefix(" "))
        elif stripped and not stripped.startswith("#"):
            break
    if not lines:
        return {}
    try:
        doc = yaml.safe_load("\n".join(lines))
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def datasets_of(header: dict[str, Any]) -> list[str]:
    tags = header.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return [str(t) for t in tags]
