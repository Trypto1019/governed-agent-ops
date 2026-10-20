"""Scripted stand-ins for the model, per role, for the offline demo.

Each script is a list of steps; step N answers the N-th assistant turn. This is
a test double, not an agent: it exists so the runtime's guarantees (allow-list,
PII filter, JSON contract, budget) can be exercised without a network.
"""
from __future__ import annotations

import json

from common.llm import Reply, call, final


def _block(events=(), memory=(), escalations=()) -> str:
    return "Summary follows.\n" + json.dumps({
        "memory_updates": list(memory), "events": list(events),
        "escalations": list(escalations)})


SCRIPTS: dict[str, list[Reply]] = {
    "finance": [
        call("metrics_read", metric="leads", days=7),
        call("query_view", view_name="budgets"),
        final(_block(
            events=[{"action": "finance_rollup", "target": "metrics_daily",
                     "result": {"window_days": 7, "note": "cost within envelopes"}}],
            memory=[{"key": "last_rollup", "value": {"ok": True}}])),
    ],
    "operator": [
        # A view that regressed and now carries free text: the PII filter blocks it.
        call("query_view", view_name="recent_feedback", limit=5),
        # A tool the operator never declared: the allow-list denies it.
        call("send_email", to="everyone", body="hello"),
        call("query_view", view_name="verifier_results", limit=20),
        final(_block(
            events=[{"action": "weekly_digest", "target": "digest",
                     "result": {"top_issue": "recent_feedback view exposes free text"}}],
            escalations=[{"tier": 2, "title": "Raise writer envelope by 30%",
                          "body": "Above the 20% Tier-1 reallocation limit; needs a human."}])),
    ],
    "compliance": [
        final(_block(events=[{"action": "artifact_checked", "target": "demo-page",
                              "result": {"blocked": False}}])),
    ],
}


def _default_script(role: str) -> list[Reply]:
    return [final(_block(events=[{"action": "note", "target": role,
                                  "result": {"msg": "nothing to do"}}]))]


def scripted_responder(role: str):
    script = SCRIPTS.get(role) or _default_script(role)

    def respond(model, messages, tools):
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        step = script[min(turn, len(script) - 1)]
        return Reply(id="", content=step.content, tool_calls=list(step.tool_calls))

    return respond
