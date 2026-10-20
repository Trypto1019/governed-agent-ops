#!/usr/bin/env python3
"""Outcome watchdog: self-healing checks on OUTCOMES, not process liveness.

Is the form actually rendering? Are leads flowing? Is the site flagged? A
process can be "up" while the funnel is dead; this watchdog asks the funnel.

Modes (run at different cadences from cron):
  --mode liveness   every 30 min: services, HTTP, nginx known-good snapshot/rollback, disk, TLS
  --mode browser    hourly:       real-browser form-render checks + injected-script scan
  --mode daily      daily:        lead outcomes, Safe Browsing status

Safe, reversible remediations (service restart, nginx rollback to the last
config that served 200s) are applied automatically and always reported.
Alerts are deduped through a state file so an issue pings once, not every run,
and a recovery message is sent when it clears.

Every external effect goes through a `Probes` object. `SystemProbes` talks to
the real host; `MockProbes` (the default for the demo and tests) is in-memory.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from approvals.approvals import ConsoleNotifier, Notifier
from common.clock import iso, now

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    base_url: str = os.environ.get("SITE_BASE_URL", "https://acme-lending.example.com")
    services: list[str] = field(default_factory=lambda: os.environ.get(
        "WATCHDOG_SERVICES", "nginx,postgresql,prequal-api").split(","))
    endpoints: list[str] = field(default_factory=lambda: ["/", "/api/health"])
    forms: list[tuple[str, str]] = field(default_factory=lambda: [("/prequal/", "#prequal-root")])
    allowed_script_hosts: list[str] = field(default_factory=lambda: os.environ.get(
        "ALLOWED_SCRIPT_HOSTS", "fonts.googleapis.com,fonts.gstatic.com").split(","))
    state_path: Path = Path(os.environ.get("WATCHDOG_STATE", str(ROOT / "var" / "watchdog_state.json")))
    disk_min_free_pct: float = 15.0
    tls_min_days: int = 14

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.base_url).hostname or ""


# ------------------------------------------------------------------ probes
class Probes(Protocol):
    def service_active(self, name: str) -> bool: ...
    def restart_service(self, name: str) -> None: ...
    def http_status(self, url: str) -> int | None: ...
    def nginx_config_ok(self) -> bool: ...
    def snapshot_nginx(self) -> None: ...
    def restore_nginx(self) -> bool: ...
    def reload_nginx(self) -> None: ...
    def disk_free_pct(self) -> float: ...
    def tls_days_left(self, host: str) -> int | None: ...
    def form_renders(self, url: str, selector: str) -> bool | None: ...
    def script_hosts(self, url: str) -> list[str]: ...
    def safe_browsing_flagged(self, host: str) -> bool | None: ...
    def leads_last_24h(self) -> int | None: ...


@dataclass
class MockProbes:
    """In-memory host. Mutate the fields to stage an incident."""
    down_services: set[str] = field(default_factory=set)
    restart_fixes: bool = True
    http: dict[str, int] = field(default_factory=dict)          # path -> status (default 200)
    nginx_ok: bool = True
    has_snapshot: bool = False
    snapshot_ok: bool = True
    free_pct: float = 60.0
    tls_days: int | None = 80
    forms_ok: dict[str, bool] = field(default_factory=dict)
    scripts: list[str] = field(default_factory=list)
    flagged: bool = False
    leads_24h: int | None = 5
    actions: list[str] = field(default_factory=list)

    def service_active(self, name): return name not in self.down_services

    def restart_service(self, name):
        self.actions.append(f"restart:{name}")
        if self.restart_fixes:
            self.down_services.discard(name)

    def http_status(self, url):
        path = urllib.parse.urlparse(url).path or "/"
        if not self.nginx_ok:
            return 502
        return self.http.get(path, 200)

    def nginx_config_ok(self): return self.nginx_ok

    def snapshot_nginx(self):
        self.actions.append("snapshot_nginx")
        self.has_snapshot = True

    def restore_nginx(self):
        if not self.has_snapshot:
            return False
        self.actions.append("restore_nginx")
        self.nginx_ok = self.snapshot_ok
        return True

    def reload_nginx(self): self.actions.append("reload_nginx")
    def disk_free_pct(self): return self.free_pct
    def tls_days_left(self, host): return self.tls_days

    def form_renders(self, url, selector):
        return self.forms_ok.get(urllib.parse.urlparse(url).path, True)

    def script_hosts(self, url): return list(self.scripts)
    def safe_browsing_flagged(self, host): return self.flagged
    def leads_last_24h(self): return self.leads_24h


class SystemProbes:  # pragma: no cover - touches the real host
    """Real implementations. Requires root for systemctl/nginx; Playwright optional."""

    def __init__(self, nginx_sites: str = "/etc/nginx/sites-available",
                 snapshot_dir: Path = ROOT / "var" / "nginx-known-good"):
        self.sites, self.snap = nginx_sites, snapshot_dir

    @staticmethod
    def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return 124, ""

    def service_active(self, name): return self._run(["systemctl", "is-active", name])[1] == "active"
    def restart_service(self, name): self._run(["systemctl", "restart", name], 90)

    def http_status(self, url):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "AcmeWatchdog/1.0"}), timeout=15) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None

    def nginx_config_ok(self): return self._run(["nginx", "-t"])[0] == 0

    def snapshot_nginx(self):
        self.snap.mkdir(parents=True, exist_ok=True)
        self._run(["rsync", "-a", "--delete", f"{self.sites}/", f"{self.snap}/"])

    def restore_nginx(self):
        if not self.snap.is_dir():
            return False
        self._run(["rsync", "-a", "--delete", f"{self.snap}/", f"{self.sites}/"])
        return True

    def reload_nginx(self): self._run(["systemctl", "reload", "nginx"])

    def disk_free_pct(self):
        total, _used, free = shutil.disk_usage("/")
        return free / total * 100

    def tls_days_left(self, host):
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=10) as s:
                with ctx.wrap_socket(s, server_hostname=host) as ss:
                    exp = datetime.strptime(ss.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z")
            return (exp.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        except Exception:
            return None

    def form_renders(self, url, selector):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
                el = page.query_selector(selector)
                return bool(el and page.eval_on_selector(selector, "e => e.children.length > 0"))
            finally:
                browser.close()

    def script_hosts(self, url):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=45000)
            srcs = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
            browser.close()
        return [urllib.parse.urlparse(s).hostname or "" for s in srcs]

    def safe_browsing_flagged(self, host):
        # Plug in the Safe Browsing Lookup API (needs SAFE_BROWSING_API_KEY).
        key = os.environ.get("SAFE_BROWSING_API_KEY")
        if not key:
            return None
        body = {"client": {"clientId": "acme-watchdog", "clientVersion": "1.0"},
                "threatInfo": {"threatTypes": ["MALWARE", "SOCIAL_ENGINEERING"],
                               "platformTypes": ["ANY_PLATFORM"], "threatEntryTypes": ["URL"],
                               "threatEntries": [{"url": f"https://{host}/"}]}}
        req = urllib.request.Request(
            "https://safebrowsing.googleapis.com/v4/threatMatches:find?key=" + key,
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return bool(json.load(r).get("matches"))
        except Exception:
            return None

    def leads_last_24h(self):
        from common.db import connect
        with connect() as db:
            return int(db.scalar("SELECT COUNT(*) FROM leads WHERE created_at > ?",
                                 (iso(now() - timedelta(hours=24)),)))


# ------------------------------------------------------------------ state + alerts
class Watchdog:
    def __init__(self, probes: Probes, notifier: Notifier | None = None,
                 cfg: Config | None = None):
        self.p = probes
        self.notify = notifier or ConsoleNotifier()
        self.cfg = cfg or Config()
        self.state: dict = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.cfg.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(self.state, indent=2, sort_keys=True))

    def alert(self, key: str, message: str) -> None:
        """Alert once per issue until it clears."""
        entry = self.state.get(key)
        if entry and entry.get("active"):
            entry["last_seen"], entry["count"] = iso(now()), entry.get("count", 1) + 1
            return
        self.state[key] = {"active": True, "first_seen": iso(now()), "last_seen": iso(now()),
                           "count": 1}
        self.notify.send(f"ALERT [{key}] {message}")

    def resolve(self, key: str, message: str | None = None) -> None:
        entry = self.state.get(key)
        if entry and entry.get("active"):
            entry["active"], entry["resolved_at"] = False, iso(now())
            if message:
                self.notify.send(f"RESOLVED [{key}] {message}")

    def action(self, message: str) -> None:
        """Autonomous remediations are always reported (never deduped)."""
        self.notify.send(f"ACTION {message}")

    def url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    # -------------------------------------------------------------- liveness
    def check_services(self) -> None:
        for svc in self.cfg.services:
            if self.p.service_active(svc):
                self.resolve(f"svc:{svc}", f"{svc} is back up")
                continue
            self.action(f"{svc} was not active; restarting")
            self.p.restart_service(svc)
            if self.p.service_active(svc):
                self.action(f"{svc} restarted successfully")
                self.resolve(f"svc:{svc}")
            else:
                self.alert(f"svc:{svc}", f"{svc} is down and auto-restart FAILED; needs a human")

    def rollback_nginx(self) -> bool:
        if not self.p.restore_nginx():
            self.alert("nginx:norollback", "nginx config broken and NO known-good snapshot")
            return False
        if self.p.nginx_config_ok():
            self.p.reload_nginx()
            self.action("rolled nginx back to the last known-good config and reloaded")
            return True
        self.alert("nginx:norollback", "nginx rollback still fails `nginx -t`")
        return False

    def check_http(self) -> None:
        all_ok = True
        for path in self.cfg.endpoints:
            status = self.p.http_status(self.url(path))
            if status != 200:
                # If the config itself is broken, roll back first, then re-test.
                if not self.p.nginx_config_ok() and self.rollback_nginx():
                    status = self.p.http_status(self.url(path))
            if status == 200:
                self.resolve(f"http:{path}", f"{path} is responding 200 again")
            else:
                all_ok = False
                self.alert(f"http:{path}", f"{path} returned {status}")
        # Snapshot ONLY a config that is both valid and serving: that is what
        # makes it "known good".
        if all_ok and self.p.nginx_config_ok():
            self.p.snapshot_nginx()

    def check_disk(self) -> None:
        pct = self.p.disk_free_pct()
        if pct < self.cfg.disk_min_free_pct:
            self.alert("disk", f"disk only {pct:.1f}% free")
        else:
            self.resolve("disk")

    def check_tls(self) -> None:
        days = self.p.tls_days_left(self.cfg.host)
        if days is None:
            self.alert("tls", "TLS check failed")
        elif days < self.cfg.tls_min_days:
            self.alert("tls", f"TLS certificate expires in {days}d")
        else:
            self.resolve("tls")

    # -------------------------------------------------------------- browser
    def check_forms(self) -> None:
        for path, selector in self.cfg.forms:
            ok = self.p.form_renders(self.url(path), selector)
            if ok is None:
                self.alert("browser:unavailable", "headless browser not available; form checks off")
            elif ok:
                self.resolve(f"form:{path}", f"{path} form is rendering again")
            else:
                self.alert(f"form:{path}", f"{path} form NOT rendering ({selector} empty): "
                                           "the funnel is dead even though HTTP is 200")
        rogue = [h for h in self.p.script_hosts(self.url("/"))
                 if h and h not in self.cfg.allowed_script_hosts
                 and not h.endswith(self.cfg.host)]
        if rogue:
            self.alert("inject:homepage", "unrecognised external script host(s): "
                       + ", ".join(sorted(set(rogue))[:5]))
        else:
            self.resolve("inject:homepage")

    # -------------------------------------------------------------- daily
    def check_lead_outcomes(self) -> None:
        n = self.p.leads_last_24h()
        if n is None:
            self.alert("leads:query", "could not query lead outcomes")
        elif n < 1:
            self.alert("leads:none", "no leads in 24h: check the funnel and traffic")
        else:
            self.resolve("leads:none", "leads are flowing again")

    def check_safe_browsing(self) -> None:
        flagged = self.p.safe_browsing_flagged(self.cfg.host)
        if flagged:
            self.alert("safebrowsing", f"Safe Browsing has FLAGGED {self.cfg.host}; investigate now")
        elif flagged is False:
            self.resolve("safebrowsing", "Safe Browsing flag cleared")

    MODES: dict[str, list[str]] = {
        "liveness": ["check_services", "check_http", "check_disk", "check_tls"],
        "browser": ["check_forms"],
        "daily": ["check_lead_outcomes", "check_safe_browsing"],
    }

    def run(self, mode: str) -> dict:
        for name in self.MODES[mode]:
            try:
                getattr(self, name)()
            except Exception as e:
                self.alert(f"watchdog:{name}", f"check {name} crashed: {type(e).__name__}")
        self.state["_last_run"] = {"mode": mode, "at": iso(now())}
        self.save()
        return self.state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(Watchdog.MODES), required=True)
    ap.add_argument("--system", action="store_true", help="use real host probes (needs root)")
    a = ap.parse_args(argv)
    probes: Probes = SystemProbes() if a.system else MockProbes()
    state = Watchdog(probes).run(a.mode)
    active = [k for k, v in state.items() if isinstance(v, dict) and v.get("active")]
    print(f"watchdog mode={a.mode} active_alerts={active or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
