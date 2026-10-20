import json

from common.llm import MockLLM, call, final
from runtime.agent_loop import extract_json_block, run_agent

BLOCK = json.dumps({"memory_updates": [{"key": "k", "value": {"v": 1}}],
                    "events": [{"action": "did_thing", "target": "x"}],
                    "escalations": []})


def scripted(*steps):
    steps = list(steps)

    def respond(model, messages, tools):
        turn = sum(1 for m in messages if m["role"] == "assistant")
        return steps[min(turn, len(steps) - 1)]
    return respond


def test_ok_run_applies_block(seeded):
    llm = MockLLM(scripted(call("metrics_read", days=7), final("done " + BLOCK)))
    r = run_agent(seeded, "finance", "rollup", llm)
    assert r.status == "ok" and r.turns == 2
    assert r.cost_usd == 2 * 0.0004                     # from generation records, not estimated
    assert seeded.scalar("SELECT COUNT(*) FROM events WHERE action='did_thing'") == 1
    assert seeded.scalar("SELECT COUNT(*) FROM memory WHERE role='finance' AND key='k'") == 1


def test_undeclared_tool_is_denied(seeded):
    llm = MockLLM(scripted(call("send_email", to="all"), final(BLOCK)))
    r = run_agent(seeded, "finance", "t", llm)
    assert r.denied_tools == ["send_email"]
    assert r.status == "ok"


def test_tool_outside_role_allow_list_is_denied(seeded):
    # compliance may only use memory_write; query_view exists but is not declared
    llm = MockLLM(scripted(call("query_view", view_name="budgets"), final(BLOCK)))
    r = run_agent(seeded, "compliance", "t", llm)
    assert r.denied_tools == ["query_view"]


def test_pii_in_tool_result_never_reaches_model(seeded):
    seen = []

    def respond(model, messages, tools):
        seen.extend(m.get("content") or "" for m in messages if m["role"] == "tool")
        turn = sum(1 for m in messages if m["role"] == "assistant")
        return call("query_view", view_name="recent_feedback") if turn == 0 else final(BLOCK)

    r = run_agent(seeded, "operator", "digest", MockLLM(respond))
    assert r.blocked_tools == ["query_view"]
    assert seen and all("example.com" not in s for s in seen)
    assert "BLOCKED" in seen[0]
    assert seeded.scalar("SELECT COUNT(*) FROM events WHERE action='compliance_block'") == 1


def test_missing_json_block_is_an_error(seeded):
    r = run_agent(seeded, "finance", "t", MockLLM(scripted(final("I ran successfully."))))
    assert r.status == "error" and "JSON block" in r.error


def test_runaway_loop_hits_max_turns(seeded):
    r = run_agent(seeded, "finance", "t", MockLLM(scripted(call("metrics_read"))))
    assert r.status == "error" and r.turns == 12


def test_budget_exhausted_on_402(seeded):
    from budget.provision_keys import MockKeyProvider
    keys = MockKeyProvider(seeded)
    keys.create("acme-paid", 0.0)
    llm = MockLLM(scripted(final(BLOCK)), charge=keys.charger("acme-paid"))
    r = run_agent(seeded, "paid", "t", llm)
    assert r.status == "budget_exhausted"
    assert seeded.scalar("SELECT COUNT(*) FROM events WHERE action='budget_exhausted'") == 1


def test_escalations_become_pending_approvals(seeded):
    blk = json.dumps({"events": [], "escalations": [{"tier": 2, "title": "Change envelope",
                                                     "body": "needs human"}]})
    run_agent(seeded, "operator", "t", MockLLM(scripted(final(blk))))
    assert seeded.one("SELECT status, tier FROM approvals") == {"status": "pending", "tier": 2}


def test_extract_json_block():
    assert extract_json_block("x " + BLOCK)["events"][0]["action"] == "did_thing"
    assert extract_json_block('{"other": 1}') is None
    assert extract_json_block("no json") is None
