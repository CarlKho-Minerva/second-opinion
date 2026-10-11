"""Fakes. No test touches the network or a real key."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from second_opinion.gateway import Action, Gateway
from second_opinion.providers import ProviderError, Reply
from second_opinion.records import RecordStore


def run(coro):
    return asyncio.run(coro)


def jev_answers(p_rev: float = 0.0, p_out: float = 0.95, score: float = 1.8, check: str = "cheap") -> dict[str, Any]:
    return {"reversible": {"type": "choice", "choice": "reversible" if p_rev >= 0.5 else "irreversible",
                           "probabilities": {"reversible": p_rev, "irreversible": 1 - p_rev}},
            "outward": {"type": "noul", "noul": p_out},
            "cost_if_wrong": {"type": "score", "score": score},
            "check": {"type": "choice", "choice": check}}


PASS = jev_answers(p_rev=1.0, p_out=0.03, score=0.1)
CHEAP_REVERSIBLE = jev_answers(p_rev=0.75, p_out=0.95, score=1.1)     # e.g. a free RSVP
CHEAP_IRREVERSIBLE = jev_answers(p_rev=0.1, p_out=0.95, score=1.1)
COSTLY = jev_answers(p_rev=0.0, p_out=0.98, score=1.9)                # e.g. a sent email


class FakeModels:
    """Scripted stand-in for ModelClient.

    script maps a family to a list of things to return in order: a JSON string, or an
    Exception to raise. The last item repeats. jev is the router answer, or an Exception.
    """

    def __init__(self, script: dict[str, list[Any]] | None = None, jev: Any = COSTLY):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.jev = jev
        self.asked: list[tuple[str, str, str]] = []      # (family, system, user)
        self.calls = 0

    def model_for(self, family: str) -> str:
        return f"fake-{family}"

    def available(self, family: str) -> bool:
        return True

    async def systemone(self, state, questions) -> Reply:
        self.calls += 1
        if isinstance(self.jev, Exception):
            raise self.jev
        return Reply("", "typesafe", "typesafe", "fake-jev", 5, 300, 20, 0.00001, raw=self.jev)

    async def complete(self, family: str, system: str, user: str, max_tokens: int = 400) -> Reply:
        self.calls += 1
        self.asked.append((family, system, user))
        queue = self.script.get(family)
        if not queue:
            raise ProviderError(f"{family}: not scripted")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return Reply(item, family, family, f"fake-{family}", 7, 200, 30, 0.0001)


APPROVE = '{"verdict": "approve", "reason": "The evidence supports it."}'
REJECT = '{"verdict": "reject", "reason": "The title in the evidence is a different event."}'


def make_action(proposer: str = "google", tool: str = "email.send") -> Action:
    return Action(id="t1", tool=tool, args={"to": "a@b.example", "body": "hello"},
                  task="Send the note.", evidence=[{"source": "email", "text": "Please reply to a@b.example."}],
                  proposer_family=proposer, rationale="SECRET-RATIONALE the address is on file")


class Spy:
    """Counts real tool executions."""

    def __init__(self):
        self.ran: list[str] = []

    def __call__(self, name, args):
        self.ran.append(name)
        return {"simulated": True, "tool": name, "result": "SIMULATED"}


@pytest.fixture
def spy():
    return Spy()


@pytest.fixture
def store(tmp_path):
    return RecordStore(tmp_path / "decisions.jsonl")


def make_gateway(models, store, spy, mode="cross", **kw) -> Gateway:
    return Gateway(models, store, mode=mode, execute=spy, **kw)
