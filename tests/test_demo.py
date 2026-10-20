"""The whole demo runs offline and shows each governance behaviour."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_make_demo_end_to_end(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("GOV_OPS_NOW", "DATABASE_URL")}
    env.update({"LLM_PROVIDER": "mock", "WATCHDOG_STATE": str(ROOT / "var" / "watchdog_state.json")})
    r = subprocess.run([sys.executable, "demo/run_demo.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    out = r.stdout
    assert r.returncode == 0, r.stderr[-2000:]
    assert "status=suspicious" in out                      # zero events is not ok
    assert "-> SUSPENDED" in out                           # two failures auto-disable
    assert "status=budget_exhausted" in out                # 402 at the key
    assert "PII blocks:             [('operator', 'email')]" in out
    assert "denied tool calls:      [['send_email']]" in out
    assert "EXPIRED after 49h" in out and "Not executed" in out
    assert "rolled nginx back" in out
    assert "live sales: 0" in out
    assert "vendor-x/sloppy-candidate    finance     FAIL" in out
