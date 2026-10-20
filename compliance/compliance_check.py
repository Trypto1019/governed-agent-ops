#!/usr/bin/env python3
"""compliance_check.py - the gate. Deterministic first; a model only for judgment.

  python -m compliance.compliance_check --file page.html --type page
  python -m compliance.compliance_check --stdin --type email
  python -m compliance.compliance_check --eval-set [--no-llm]

Exit 0 = pass, 1 = BLOCKED. Callers must abort on 1.

Rules live in rules/*.yaml as data. Regex, "absent" and custom handlers run
with no model at all. Only `check: llm` rules call a model, and then with the
artifact text alone. A judgment rule that cannot be evaluated FAILS CLOSED.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable

from compliance import yaml_subset

HERE = Path(__file__).resolve().parent
RULES_DIR = HERE / "rules"
GOLDEN = HERE / "golden" / "artifacts.json"

# (violation: True/False/None-if-unevaluated, why)
Judge = Callable[[dict, str], "tuple[bool | None, str]"]


def site_host() -> str:
    return os.environ.get("SITE_HOST", "acme-lending.example.com")


def load_rules(rules_dir: Path = RULES_DIR) -> tuple[list[dict], str | None]:
    rules, version = [], None
    for p in sorted(rules_dir.glob("*.yaml")):
        d = yaml_subset.load(p.read_text())
        version = d.get("version", version)
        rules += d.get("rules", [])
    return rules, version


# ------------------------------------------------------------------ handlers
def affiliate_needs_sponsored(body: str, kind: str) -> str | None:
    """An outbound link to another host needs rel=sponsored AND a visible disclosure."""
    own = re.escape(site_host())
    anchors = re.findall(rf'<a\b[^>]*href=["\']https?://(?!(?:www\.)?{own})[^"\']+["\'][^>]*>',
                         body, re.I)
    if not anchors:
        return None
    missing = [a for a in anchors if "sponsored" not in a.lower()]
    if missing:
        return f'{len(missing)} external link(s) without rel="sponsored"'
    if not re.search(r"(?i)(disclosure|we may earn|affiliate|commission)", body):
        return "sponsored links present but no visible disclosure on the page"
    return None


def apr_needs_disclosure(body: str, kind: str) -> str | None:
    if not re.search(r"\b\d{1,3}(\.\d+)?\s*%\s*APR|\bAPR\b.{0,20}\d{1,3}(\.\d+)?\s*%", body, re.I):
        return None
    if re.search(r"(?i)(apr (range|disclosure)|rates? (vary|range)|representative example|"
                 r"depend(s|ing) on creditworthiness)", body):
        return None
    return "APR advertised without a rate-range or disclosure statement"


def bulk_needs_approval(body: str, kind: str) -> str | None:
    m = re.search(r"\b(\d{3,6})\s+(recipients|applicants|leads|people|subscribers)", body, re.I)
    if not m or int(m.group(1)) <= 100:
        return None
    if re.search(r"approval[_ ]?id|approvals?\s*#?\d+", body, re.I):
        return None
    return f"send to {int(m.group(1))} recipients with no approval id referenced"


HANDLERS: dict[str, Callable[[str, str], str | None]] = {
    "affiliate_needs_sponsored": affiliate_needs_sponsored,
    "apr_needs_disclosure": apr_needs_disclosure,
    "bulk_needs_approval": bulk_needs_approval,
}


# ------------------------------------------------------------------ judges
def mock_judge(rule: dict, body: str) -> tuple[bool | None, str]:
    """Offline stand-in for the model on the two judgment rules. NOT a real
    classifier: it exists so the gate's plumbing (and fail-closed) is testable."""
    if rule["id"] == "named_lender_claim":
        hit = re.search(r"\b[A-Z][a-z]+ (Lending|Bank|Credit|Loans)\b.{0,60}"
                        r"\b(approves more|is the (fastest|best|cheapest)|beats)\b", body)
        return bool(hit), "comparative claim about a named lender" if hit else ""
    if rule["id"] == "numeric_claim_without_citation":
        stat = re.search(r"\b\d{1,3}%\s+of\s+(Americans|borrowers|people|credit reports|consumers)",
                         body, re.I)
        cited = re.search(r"(?i)(source:|according to|our data|survey of our)", body)
        return bool(stat and not cited), "uncited world statistic" if stat and not cited else ""
    return None, f"no mock judgment for {rule['id']}"


def openrouter_judge(rule: dict, body: str) -> tuple[bool | None, str]:
    """Artifact text only: no tools, no data. Errors are returned as None."""
    key = os.environ.get("OPENROUTER_KEY_COMPLIANCE")
    if not key:
        return None, "no compliance key"
    from common.llm import OpenRouterLLM
    model = json.loads((HERE.parent / "roles" / "compliance" / "role.json").read_text())["model"]
    try:
        r = OpenRouterLLM(key, timeout=90).chat(model, [
            {"role": "system", "content": "You are a compliance gate. Answer ONLY with JSON "
                                          '{"violation": true|false, "why": "<one sentence>"}.'},
            {"role": "user", "content": f"RULE: {rule['prompt']}\n\nARTIFACT:\n{body[:8000]}"}])
        t = r.content or ""
        d = json.loads(t[t.index("{"): t.rindex("}") + 1])
        return bool(d.get("violation")), d.get("why", "")
    except Exception as e:
        return None, f"llm error: {type(e).__name__}"


def default_judge() -> Judge:
    return mock_judge if os.environ.get("LLM_PROVIDER", "mock") == "mock" else openrouter_judge


# ------------------------------------------------------------------ gate
def check(body: str, kind: str, judge: Judge | None = None,
          rules: list[dict] | None = None) -> tuple[list[dict], str | None]:
    """Return (findings, rules_version). `judge=None` skips judgment rules."""
    version = None
    if rules is None:
        rules, version = load_rules()
    findings = []
    for r in rules:
        if kind not in r.get("scope", []):
            continue
        hit = None
        if r["check"] == "regex" and re.search(r["pattern"], body):
            hit = f"matched /{r['pattern']}/"
        elif r["check"] == "absent" and not re.search(r["pattern"], body):
            hit = f"required pattern /{r['pattern']}/ absent"
        elif r["check"] == "custom":
            hit = HANDLERS[r["handler"]](body, kind)
        elif r["check"] == "llm" and judge is not None:
            violation, why = judge(r, body)
            if violation is True:
                hit = why or "judged a violation"
            elif violation is None:
                hit = f"UNEVALUATED ({why}) - failing closed"
        if hit:
            findings.append({"rule": r["id"], "severity": r["severity"], "detail": hit})
    return findings, version


def is_blocked(findings: list[dict]) -> bool:
    return any(f["severity"] == "block" for f in findings)


def eval_set(judge: Judge | None, path: Path = GOLDEN) -> dict:
    """Precision/recall of the gate against the labelled golden artifacts."""
    rules, _ = load_rules()
    llm_rules = {r["id"] for r in rules if r["check"] == "llm"}
    tp = fp = fn = tn = skipped = 0
    misses = []
    for case in json.loads(path.read_text()):
        truth = case["violation"]
        if judge is None and truth in llm_rules:
            skipped += 1                    # judgment cases need a judge
            continue
        f, _ = check(case["body"], case["type"], judge, rules)
        got = f[0]["rule"] if f else None
        if truth and got == truth:
            tp += 1
        elif truth and got:
            fp += 1; fn += 1; misses.append((case["id"], truth, got))
        elif truth:
            fn += 1; misses.append((case["id"], truth, None))
        elif got:
            fp += 1; misses.append((case["id"], None, got))
        else:
            tn += 1
    return {"precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn, "skipped": skipped, "misses": misses}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--type", default="page")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--eval-set", action="store_true")
    a = ap.parse_args(argv)
    judge = None if a.no_llm else default_judge()

    if a.eval_set:
        s = eval_set(judge)
        print(f"gate: precision={s['precision']:.3f} recall={s['recall']:.3f} "
              f"(tp={s['tp']} fp={s['fp']} fn={s['fn']} tn={s['tn']} "
              f"skipped_judgment={s['skipped']})")
        for cid, truth, got in s["misses"][:12]:
            print(f"  miss {cid}: truth={truth} got={got}")
        return 0 if s["precision"] >= 0.95 and s["recall"] >= 0.95 else 1

    body = Path(a.file).read_text() if a.file else sys.stdin.read()
    findings, version = check(body, a.type, judge)
    print(json.dumps({"rules_version": version, "type": a.type, "findings": findings,
                      "blocked": is_blocked(findings)}, indent=1))
    return 1 if is_blocked(findings) else 0


if __name__ == "__main__":
    sys.exit(main())
