from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

T0 = "2026-10-01T12:00:00+00:00"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own SQLite file, a pinned clock and the mock provider."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.sqlite'}")
    monkeypatch.setenv("GOV_OPS_NOW", T0)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("WATCHDOG_STATE", str(tmp_path / "watchdog_state.json"))
    for var in ("ROUTER_LIVE", "VERIFIER_HTTP", "CHAT_WEBHOOK_URL", "APPROVERS"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def db():
    from common.db import connect
    d = connect()
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
def seeded(db):
    from demo.seed import seed
    seed(db)
    return db
