"""A tiny loader for the YAML subset used by rules/*.yaml.

The repo is stdlib-only by design, so this parses exactly what the rules file
uses: top-level scalars, one top-level list of flat mappings, single/double
quoted strings, flow lists `[a, b]`, and folded `>` blocks. If PyYAML is
installed it is used instead (tests assert both agree).
"""
from __future__ import annotations

from typing import Any


def _scalar(v: str) -> Any:
    v = v.strip()
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("''", "'")
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1].replace('\\"', '"')
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        return v


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def loads(text: str) -> dict:
    lines = text.splitlines()
    doc: dict[str, Any] = {}
    current_list: list | None = None
    item: dict | None = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        indent = _indent(raw)
        if indent == 0:
            key, _, val = s.partition(":")
            if val.strip():
                doc[key] = _scalar(val)
            else:
                current_list = doc[key] = []
            i += 1
            continue
        if s.startswith("- "):
            if current_list is None:
                raise ValueError(f"list item outside a list on line {i + 1}")
            item = {}
            current_list.append(item)
            s, indent = s[2:], indent + 2
        if item is None:
            raise ValueError(f"unexpected indentation on line {i + 1}")
        key, _, val = s.partition(":")
        val = val.strip()
        if val in (">", "|"):
            block: list[str] = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > indent):
                block.append(lines[i].strip())
                i += 1
            item[key] = (" ".join(b for b in block if b) if val == ">" else "\n".join(block)) + "\n"
            continue
        item[key] = _scalar(val)
        i += 1
    return doc


def load(text: str) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        return loads(text)
