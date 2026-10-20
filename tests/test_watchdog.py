from approvals.approvals import ListNotifier
from watchdog.watchdog import Config, MockProbes, Watchdog


def make(tmp_path, **probe_kwargs):
    probes, notes = MockProbes(**probe_kwargs), ListNotifier()
    wd = Watchdog(probes, notes, Config(state_path=tmp_path / "state.json"))
    return wd, probes, notes


def test_healthy_run_snapshots_known_good(tmp_path):
    wd, probes, notes = make(tmp_path)
    wd.run("liveness")
    assert "snapshot_nginx" in probes.actions and notes.sent == []


def test_broken_config_rolls_back_to_known_good(tmp_path):
    wd, probes, notes = make(tmp_path, has_snapshot=True, nginx_ok=False)
    wd.run("liveness")
    assert probes.actions[:2] == ["restore_nginx", "reload_nginx"]
    assert any("rolled nginx back" in m for m in notes.sent)
    assert not any(m.startswith("ALERT [http") for m in notes.sent)


def test_no_snapshot_means_human(tmp_path):
    wd, probes, notes = make(tmp_path, nginx_ok=False)
    wd.run("liveness")
    assert any("NO known-good snapshot" in m for m in notes.sent)
    assert "snapshot_nginx" not in probes.actions             # never snapshot a bad config


def test_alert_dedupes_and_resolves(tmp_path):
    wd, probes, notes = make(tmp_path, forms_ok={"/prequal/": False})
    wd.run("browser")
    wd.run("browser")
    alerts = [m for m in notes.sent if m.startswith("ALERT [form")]
    assert len(alerts) == 1 and wd.state["form:/prequal/"]["count"] == 2
    probes.forms_ok["/prequal/"] = True
    wd.run("browser")
    assert any(m.startswith("RESOLVED [form") for m in notes.sent)


def test_service_restart_and_failed_restart(tmp_path):
    wd, probes, notes = make(tmp_path, down_services={"nginx"})
    wd.check_services()
    assert "restart:nginx" in probes.actions and not any("FAILED" in m for m in notes.sent)
    wd2, _, notes2 = make(tmp_path, down_services={"prequal-api"}, restart_fixes=False)
    wd2.check_services()
    assert any("auto-restart FAILED" in m for m in notes2.sent)


def test_daily_outcomes(tmp_path):
    wd, probes, notes = make(tmp_path, flagged=True, leads_24h=0,
                             scripts=["cdn.unknown-tracker.example.net"])
    wd.run("daily")
    wd.run("browser")
    text = " ".join(notes.sent)
    assert "Safe Browsing has FLAGGED" in text and "no leads in 24h" in text
    assert "unrecognised external script" in text


def test_state_file_written(tmp_path):
    wd, _, _ = make(tmp_path)
    wd.run("daily")
    assert (tmp_path / "state.json").exists()
