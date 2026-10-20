"""LLM providers behind one small interface.

* `OpenRouterLLM`: real calls (stdlib urllib, OpenAI-compatible API).
* `MockLLM`: offline, deterministic. A *responder* function plays the model.

Both raise `BudgetExhausted` on HTTP 402, which is how a per-role key with a
spend limit tells you the envelope is gone. Enforcement lives at the key, not
in this code.
"""
from __future__ import annotations

import itertools
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


class BudgetExhausted(Exception):
    """The provider returned 402: this key's spend limit is reached."""
    status = 402


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"

    def as_message(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class Reply:
    id: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str | None = None

    def as_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [c.as_message() for c in self.tool_calls]
        return msg


class LLM(Protocol):
    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
             fallback: str | None = None) -> Reply: ...

    def generation_cost(self, generation_id: str) -> float | None: ...


# ------------------------------------------------------------------ real
class OpenRouterLLM:
    def __init__(self, api_key: str, timeout: float = 120.0):
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{OPENROUTER_BASE}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 402:
                raise BudgetExhausted("provider returned 402 (key spend limit reached)") from e
            raise

    def chat(self, model, messages, tools=None, fallback=None) -> Reply:
        body = {
            "model": model,
            "messages": messages,
            # Fallback is a DIFFERENT provider: an outage degrades the role, it
            # does not stop it.
            "models": [m for m in (model, fallback) if m],
            # Never route to providers that log or train on inputs.
            "provider": {"data_collection": "deny"},
        }
        if tools:
            body["tools"] = tools
        d = self._request("POST", "/chat/completions", body)
        msg = d["choices"][0]["message"]
        calls = [ToolCall(c["id"], c["function"]["name"], c["function"].get("arguments") or "{}")
                 for c in (msg.get("tool_calls") or [])]
        return Reply(id=d.get("id", ""), content=msg.get("content"), tool_calls=calls,
                     model=d.get("model"))

    def generation_cost(self, generation_id: str) -> float | None:
        """Exact cost from the generation endpoint, not a usage estimate.

        The record is populated asynchronously; an eager fetch 404s and the cost
        is silently lost. Cost IS the budget signal, so back off generously.
        """
        for attempt in range(6):
            try:
                d = self._request("GET", f"/generation?id={generation_id}")
                c = (d.get("data") or {}).get("total_cost")
                if c is not None:
                    return float(c)
            except BudgetExhausted:
                raise
            except Exception:
                pass
            time.sleep(min(2 ** attempt, 16))
        return None


# ------------------------------------------------------------------ mock
Responder = Callable[[str, list[dict], "list[dict] | None"], Reply]


class MockLLM:
    """Offline model. `responder(model, messages, tools)` decides the reply.

    `charge(cost)` is called before each reply; wire it to a mock key store to
    simulate a spend-limited key returning 402.
    """

    def __init__(self, responder: Responder, cost_per_call: float = 0.0004,
                 charge: Callable[[float], None] | None = None,
                 latency_ms: int = 0):
        self.responder = responder
        self.cost_per_call = cost_per_call
        self.charge = charge
        self.latency_ms = latency_ms
        self._costs: dict[str, float] = {}
        self._ids = itertools.count(1)

    def chat(self, model, messages, tools=None, fallback=None) -> Reply:
        if self.charge:
            self.charge(self.cost_per_call)   # may raise BudgetExhausted
        reply = self.responder(model, messages, tools)
        reply.id = reply.id or f"gen-mock-{next(self._ids)}"
        reply.model = reply.model or model
        self._costs[reply.id] = self.cost_per_call
        return reply

    def generation_cost(self, generation_id: str) -> float | None:
        return self._costs.get(generation_id)


def final(text: str) -> Reply:
    return Reply(id="", content=text)


def call(name: str, **args: Any) -> Reply:
    return Reply(id="", tool_calls=[ToolCall(id=f"call-{name}", name=name,
                                             arguments=json.dumps(args))])
