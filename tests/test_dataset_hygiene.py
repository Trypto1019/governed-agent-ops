"""Repository hygiene: fails on real-looking contact data or any scrub-list term.

* Phone numbers must be in the reserved fictional range (555-01xx).
* Email addresses must use example.com.
* No IPv4 literals other than loopback/unspecified.
* No private scrub term (brands, domains, people, host paths). The list lives
  outside the repo; set PORTFOLIO_SCRUB_TERMS to its path to enable that check.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "var", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".sql", ".json", ".yaml", ".yml", ".toml", ".txt", ".example",
                 ".cfg", ".ini", ".sh", ""}

def _load_private_terms() -> tuple[list[str], list[str], list[str]]:
    """Read private scrub terms from the file named by PORTFOLIO_SCRUB_TERMS.

    The terms live outside the repository so this public file never lists what
    it is meant to keep out. Without the variable, only the generic checks run.
    Line format: plain = forbidden term, "owner:" = name allowed only in
    README/LICENSE, "re:" = regex, "#" = comment.
    """
    path = os.environ.get("PORTFOLIO_SCRUB_TERMS", "")
    if not path or not Path(path).is_file():
        return [], [], []
    terms, owner, regexes = [], [], []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("owner:"):
            owner.append(line[len("owner:"):])
        elif line.startswith("re:"):
            regexes.append(line[len("re:"):])
        else:
            terms.append(line)
    return terms, owner, regexes


_TERMS, _OWNER, _REGEXES = _load_private_terms()
SCRUB = [t.lower() for t in _TERMS + _OWNER]
SCRUB_REGEXES = _REGEXES
# The LICENSE copyright line is the only place the owner's name may appear.
COPYRIGHT_LINE = re.compile(r"^Copyright \(c\) \d{4} .*$", re.M)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
PHONE = re.compile(r"(?<![\w.])(?:\+?1[-. ]?)?\(?(\d{3})\)?[-. ]?(\d{3})[-. ]?(\d{4})(?![\w])")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}


def repo_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            if p.suffix in TEXT_SUFFIXES or p.name in {"Makefile", "LICENSE", ".gitignore"}:
                out.append(p)
    return sorted(out)


FILES = repo_files()


def test_scan_covers_the_repo():
    assert len(FILES) > 30


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_file_is_clean(path: Path):
    text = path.read_text(errors="replace")
    if path.name == "LICENSE":
        text = COPYRIGHT_LINE.sub("", text)
    low = text.lower()
    problems = [f"scrub term #{i}" for i, term in enumerate(SCRUB) if term in low]
    problems += [f"scrub regex #{i}" for i, rx in enumerate(SCRUB_REGEXES) if re.search(rx, text, re.I)]
    for m in EMAIL.finditer(text):
        if m.group(1).lower() != "example.com":
            problems.append(f"email domain {m.group(1)}")
    for m in PHONE.finditer(text):
        area, exch, line = m.groups()
        if not (area == "555" and exch.startswith("01")) and not (exch == "555" and line.startswith("01")):
            problems.append(f"phone-like {m.group(0)!r}")
    for m in IPV4.finditer(text):
        ip = ".".join(m.groups())
        if ip not in ALLOWED_IPS and all(int(x) <= 255 for x in m.groups()):
            problems.append(f"ip {ip}")
    assert not problems, f"{path.relative_to(ROOT)}: {problems}"
