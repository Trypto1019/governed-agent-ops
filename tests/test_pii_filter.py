import pytest

from runtime.pii_filter import blocked_payload, redact, scan_for_pii


@pytest.mark.parametrize("text,kind", [
    ('{"comment": "reach me at alex.rivera@example.com"}', "email"),
    ('{"gov_id": "900-12-0001"}', "ssn"),
    ('{"gov_id": "900120001"}', "ssn9"),
    ('{"phone": "+1-555-0142"}', None),            # 7-digit fictional form is not a full number
    ('{"phone": "(555) 010-0142"}', "phone"),
    ('{"tier": "good", "n": 12, "cost": 0.0004}', None),
])
def test_scan(text, kind):
    assert scan_for_pii(text) == kind


def test_redact_replaces_every_match():
    out = redact("a@example.com and 900-12-0001")
    assert "example.com" not in out and "900-12" not in out
    assert "[EMAIL]" in out and "[SSN]" in out


def test_blocked_payload_names_the_kind_not_the_value():
    p = blocked_payload("query_view", "email")
    assert "email" in p["error"] and "@" not in p["error"]
