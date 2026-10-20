import pytest

from budget.provision_keys import MockKeyProvider, passing_models, sync, unqualified_reason
from common.clock import now_iso
from common.llm import BudgetExhausted


def qualify(db, model, role, passed=True, ran_at=None):
    db.execute("INSERT INTO model_evals (model, role, eval_version, ran_at, pass) "
               "VALUES (?,?,?,?,?)", (model, role, "t", ran_at or now_iso(), passed))
    db.commit()


def test_refuses_role_without_passing_eval(seeded):
    out = sync(seeded, MockKeyProvider(seeded), ["finance"])
    assert out["finance"].startswith("refused: no passing eval")
    assert MockKeyProvider(seeded).list_keys() == {}


def test_creates_key_at_envelope_when_qualified(seeded):
    qualify(seeded, "vendor-b/flash-lite", "finance")
    qualify(seeded, "vendor-c/flash", "finance")
    out = sync(seeded, MockKeyProvider(seeded), ["finance"])
    assert out["finance"] == "created limit=20.00"
    assert MockKeyProvider(seeded).list_keys()["acme-finance"]["limit"] == 20.0
    # second sync updates rather than duplicates
    assert sync(seeded, MockKeyProvider(seeded), ["finance"])["finance"].startswith("updated")


def test_failed_or_stale_eval_does_not_qualify(seeded):
    qualify(seeded, "vendor-b/flash-lite", "finance", passed=False)
    qualify(seeded, "vendor-c/flash", "finance", ran_at="2026-01-01T00:00:00+00:00")
    assert passing_models(seeded) == set()
    assert "vendor-b/flash-lite" in unqualified_reason("finance", passing_models(seeded))


def test_no_budget_row_is_skipped(seeded):
    assert sync(seeded, MockKeyProvider(seeded), ["writer"])["writer"].startswith("skipped")


def test_mock_gateway_returns_402_at_limit(db):
    keys = MockKeyProvider(db)
    keys.create("acme-x", 0.001)
    charge = keys.charger("acme-x")
    charge(0.0004)
    charge(0.0004)
    with pytest.raises(BudgetExhausted):
        charge(0.0004)
    assert keys.list_keys()["acme-x"]["usage"] == pytest.approx(0.0008)
