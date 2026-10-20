#!/usr/bin/env python3
"""agent_loop.py <role> <task> [--mock] - the tool-calling runtime for one role run.

Loop: messages -> chat(tools=role_tools) -> execute tool calls -> repeat until
the model stops or max_turns (role.json, default 12). Wall-clock timeout 20m.

Guarantees:
  * A role may only call tools declared in roles/<role>/role.json.
  * Every tool result is PII-scanned before it reaches the model; a hit is
    replaced with a refusal and logged as a `compliance_block` event.
  * The final message must contain the structured JSON block; it is parsed in
    code, not trusted. Missing or invalid -> the run is recorded as an error.
  * Cost comes from the provider's generation record by id, not an estimate.
  * A 402 from the provider (the role's key hit its spend limit) is recorded
    as `budget_exhausted`, and the run stops. There is no soft limit.

Exit codes: 0 ok, 1 error, 2 bad config, 3 no key, 4 budget exhausted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.clock import now_iso
from common.db import DB, connect, dumps
from common.events import log_event
from common.llm import LLM, BudgetExhausted, OpenRouterLLM
from runtime import tools as T
from runtime.pii_filter import blocked_payload, scan_for_pii

ROLES_DIR = Path(__file__).resolve().parent.parent / "roles"
DEFAULT_MAX_TURNS = 12
WALL_TIMEOUT_S = 20 * 60
EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_NO_KEY, EXIT_BUDGET = 0, 1, 2, 3, 4

OUTPUT_CONTRACT = (
    "\n\n## Required output\nEnd every run with a JSON block: "
    '{"memory_updates": [{"key": "...", "value": {}}], '
    '"events": [{"action": "...", "target": "...", "params": {}, "result": {}}], '
    '"escalations": [{"tier": 2, "title": "...", "body": "..."}]}')


@dataclass
class RunResult:
    status: str                       # ok | error | budget_exhausted
    turns: int = 0
    cost_usd: float | None = None
    error: str | None = None
    block: dict | None = None
    blocked_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    event_id: int | None = None


def load_role(role: str, roles_dir: Path = ROLES_DIR) -> dict:
    """model / fallback_model / max_turns / tools live in roles/<role>/role.json."""
    p = roles_dir / role / "role.json"
    return json.loads(p.read_text()) if p.exists() else {}


def load_charter(role: str, roles_dir: Path = ROLES_DIR) -> str:
    p = roles_dir / role / "CHARTER.md"
    return p.read_text() if p.exists() else f"You are the {role} agent."


def extract_json_block(text: str | None) -> dict | None:
    """The final message MUST contain the structured block. Parsed, not trusted."""
    if not text:
        return None
    try:
        block = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return None
    if not isinstance(block, dict) or not any(
            k in block for k in ("memory_updates", "events", "escalations")):
        return None
    return block


def _execute_tool(ctx: T.ToolContext, allowed: list[str], name: str, raw_args: str,
                  result: RunResult) -> str:
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        args = {}
    if name not in allowed or name not in T.REGISTRY:
        result.denied_tools.append(name)
        out: Any = {"error": f"tool '{name}' is not permitted for role {ctx.role}"}
    else:
        try:
            out = T.REGISTRY[name][0](ctx, **args)
        except Exception as e:  # tool errors go back to the model, not up the stack
            out = {"error": f"{type(e).__name__}: {e}"}

    payload = json.dumps(out, default=str)
    hit = scan_for_pii(payload)
    if hit:
        # Never send PII to the model, even if a view or grant regresses.
        T.record_block(ctx, name, hit)
        result.blocked_tools.append(name)
        payload = json.dumps(blocked_payload(name, hit))
    return payload[:100_000]


def run_agent(db: DB, role: str, task: str, llm: LLM, roles_dir: Path = ROLES_DIR,
              test: bool = False) -> RunResult:
    cfg = load_role(role, roles_dir)
    model, fallback = cfg.get("model"), cfg.get("fallback_model")
    if not model:
        return RunResult(status="error", error="role.json has no model")
    max_turns = int(cfg.get("max_turns", DEFAULT_MAX_TURNS))
    allowed = list(cfg.get("tools", []))
    tool_specs = [T.REGISTRY[n][1] for n in allowed if n in T.REGISTRY]

    memory = {r["key"]: r["value"] for r in
              db.query("SELECT key, value FROM memory WHERE role = ?", (role,))}
    messages: list[dict] = [
        {"role": "system", "content": load_charter(role, roles_dir) + OUTPUT_CONTRACT},
        {"role": "user", "content": f"## Current memory\n{dumps(memory)}\n\n## Task\n{task}"},
    ]
    ctx = T.ToolContext(db, role)
    result = RunResult(status="ok")
    started, deadline = now_iso(), time.time() + WALL_TIMEOUT_S
    gen_ids: list[str] = []
    final_text: str | None = None

    while result.turns < max_turns and time.time() < deadline:
        result.turns += 1
        try:
            reply = llm.chat(model, messages, tools=tool_specs or None, fallback=fallback)
        except BudgetExhausted as e:
            result.status, result.error = "budget_exhausted", str(e)
            break
        except Exception as e:
            result.status, result.error = "error", f"api_error: {type(e).__name__}: {e}"
            break
        if reply.id:
            gen_ids.append(reply.id)
        messages.append(reply.as_message())
        if not reply.tool_calls:
            final_text = reply.content or ""
            break
        for c in reply.tool_calls:
            messages.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                             "content": _execute_tool(ctx, allowed, c.name, c.arguments, result)})

    # A genuinely-zero cost is legitimate (sub-cent calls round to 0) and must
    # not be dropped by a falsy test, or cheap runs look unmetered.
    for gid in gen_ids:
        c = llm.generation_cost(gid)
        if c is not None:
            result.cost_usd = (result.cost_usd or 0.0) + float(c)

    if result.status == "ok":
        result.block = extract_json_block(final_text)
        if result.block is None:
            result.status = "error"
            result.error = ("max_turns reached without a final answer" if final_text is None
                            else "final message did not contain a valid JSON block")

    result.event_id = log_event(
        db, role, "budget_exhausted" if result.status == "budget_exhausted" else "run", task,
        {"started": started, "model": model, "fallback": fallback,
         "turns": result.turns, "test": test},
        {"status": result.status, "error": result.error,
         "summary": (final_text or "")[:4000], "pii_blocks": result.blocked_tools,
         "denied_tools": result.denied_tools},
        result.cost_usd)

    if result.status == "ok" and result.block:
        _apply_block(db, ctx, result.block)
    return result


def _apply_block(db: DB, ctx: T.ToolContext, block: dict) -> None:
    for ev in block.get("events", []) or []:
        log_event(db, ctx.role, ev.get("action", "note"), ev.get("target"),
                  ev.get("params"), ev.get("result"))
    for mu in block.get("memory_updates", []) or []:
        if "key" in mu:
            T.memory_write(ctx, mu["key"], mu.get("value", {}))
    # Escalations become approval rows: a human decides, and silence is a "no".
    from approvals.approvals import create
    for esc in block.get("escalations", []) or []:
        if isinstance(esc, str):
            esc = {"tier": 2, "title": esc[:120], "body": esc}
        create(db, ctx.role, int(esc.get("tier", 2)), esc.get("title", "escalation"),
               esc.get("body", ""), sticky=bool(esc.get("sticky")))


def build_llm(role: str, db: DB, mock: bool) -> LLM | None:
    if mock:
        from budget.provision_keys import MockKeyProvider
        from runtime.mock_scripts import scripted_responder
        from common.llm import MockLLM
        keys = MockKeyProvider(db)
        name = f"acme-{role}"
        if name not in keys.list_keys():
            return None          # no provisioned key -> the role does not run
        return MockLLM(scripted_responder(role), charge=keys.charger(name))
    key = os.environ.get(f"OPENROUTER_KEY_{role.upper()}")
    return OpenRouterLLM(key) if key else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("role")
    ap.add_argument("task")
    ap.add_argument("--mock", action="store_true", help="offline scripted model")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args(argv)
    mock = a.mock or os.environ.get("LLM_PROVIDER", "mock") == "mock"

    with connect() as db:
        if not load_role(a.role).get("model"):
            print(f"[{a.role}] role.json missing or has no model", file=sys.stderr)
            return EXIT_CONFIG
        llm = build_llm(a.role, db, mock)
        if llm is None:
            print(f"[{a.role}] no provisioned key for this role - not running", file=sys.stderr)
            return EXIT_NO_KEY
        r = run_agent(db, a.role, a.task, llm, test=a.test)
    print(f"[{a.role}] {r.status} turns={r.turns} cost={r.cost_usd} event={r.event_id}"
          + (f" err={r.error}" if r.error else "")
          + (f" pii_blocked={r.blocked_tools}" if r.blocked_tools else "")
          + (f" denied={r.denied_tools}" if r.denied_tools else ""))
    return {"ok": EXIT_OK, "budget_exhausted": EXIT_BUDGET}.get(r.status, EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
