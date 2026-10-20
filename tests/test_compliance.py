import pytest

from compliance import yaml_subset
from compliance.compliance_check import (RULES_DIR, check, eval_set, is_blocked, load_rules,
                                         mock_judge)


def rules_hit(body, kind, judge=None):
    findings, _ = check(body, kind, judge)
    return {f["rule"] for f in findings}


def test_rules_load_with_version():
    rules, version = load_rules()
    assert version and len(rules) >= 10
    assert {r["check"] for r in rules} == {"regex", "absent", "custom", "llm"}


def test_subset_parser_matches_pyyaml_when_available():
    yaml = pytest.importorskip("yaml")
    text = (RULES_DIR / "core.yaml").read_text()
    assert yaml_subset.loads(text) == yaml.safe_load(text)


@pytest.mark.parametrize("body,kind,rule", [
    ('{"aggregateRating": {"ratingValue": "4.9"}}', "schema", "fabricated_rating"),
    ('<a href="[partner link]">go</a>', "page", "placeholder_href"),
    ('<a href="#" class="cta">go</a>', "page", "dead_href_hash"),
    ("Finish here: https://acme-lending.example.com/resume", "email", "missing_cta_token"),
    ('<a href="https://partner.example.org/x">x</a>', "page", "affiliate_without_sponsored"),
    ("<form><input name=email></form>", "form", "form_without_tcpa_consent"),
    ("Loans from 5.99% APR", "page", "apr_without_disclosure"),
    ("Guaranteed approval today", "page", "guaranteed_approval_claim"),
    ("Send to 500 recipients {cta_url}", "email", "bulk_send_without_approval"),
])
def test_each_deterministic_rule(body, kind, rule):
    assert rule in rules_hit(body, kind)


def test_own_domain_links_need_no_sponsored():
    assert "affiliate_without_sponsored" not in rules_hit(
        '<a href="https://acme-lending.example.com/apply">apply</a>', "page")


def test_apr_with_disclosure_passes():
    assert not rules_hit("From 5.99% APR. Rates vary by lender.", "page")


def test_judgment_rules_only_with_a_judge():
    body = "<p>79% of credit reports have errors.</p>"
    assert not rules_hit(body, "page")                       # deterministic only
    assert "numeric_claim_without_citation" in rules_hit(body, "page", mock_judge)


def test_unevaluated_judgment_fails_closed():
    findings, _ = check("<p>hello</p>", "page", judge=lambda r, b: (None, "timeout"))
    assert findings and all("failing closed" in f["detail"] for f in findings)
    assert is_blocked(findings)


def test_golden_set_precision_recall():
    det = eval_set(None)
    assert det["precision"] >= 0.95 and det["recall"] >= 0.95 and det["skipped"] == 2
    full = eval_set(mock_judge)
    assert full["precision"] == 1.0 and full["recall"] == 1.0
