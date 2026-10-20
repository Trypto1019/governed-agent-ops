"""PII filter applied to EVERY tool result before the model sees it.

This is the second barrier. The first is that agents can only read allow-listed
views (runtime/tools.py) that a DB grant also restricts. Views regress: someone
adds a free-text column and an email rides along. When that happens the filter
withholds the whole result and logs a `compliance_block` event, which makes the
regression visible instead of silent.

The patterns are deliberately blunt. A false positive costs one tool call; a
false negative sends a customer's identity to a third-party model.
"""
from __future__ import annotations

import re

PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"), "email"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    (re.compile(r"\b\d{9}\b"), "ssn9"),
    (re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)"), "phone"),
]


def scan_for_pii(text: str) -> str | None:
    """Return the kind of the first PII pattern found, or None."""
    for pattern, kind in PII_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def redact(text: str) -> str:
    """Replace every match with a typed placeholder (for logs, never for tool results)."""
    for pattern, kind in PII_PATTERNS:
        text = pattern.sub(f"[{kind.upper()}]", text)
    return text


def blocked_payload(tool: str, kind: str) -> dict:
    return {"error": f"BLOCKED: result of '{tool}' contained PII ({kind}) and was withheld. "
                     "This is a bug in the view or grant; it has been logged."}
