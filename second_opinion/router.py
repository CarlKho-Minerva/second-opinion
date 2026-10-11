"""The system-one router. It looks at one proposed action and answers four small questions:
can it be undone, does it reach anyone else, how bad is a mistake, how easy is it to check.

Order of preference: Jev (TypeSafe), then the cheapest fast chat model, then a fixed
fail-closed answer. The record always says which one actually answered.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from typing import Any

from . import config, tools
from .providers import ModelClient, ProviderError

# Thresholds live in code so they can be read, tested and changed without a model.
P_REVERSIBLE_MIN = 0.70     # below this we do not trust "reversible"
P_OUTWARD_MIN = 0.30        # at or above this we treat the action as reaching someone else
COSTLY_SCORE_MIN = 1.50     # 0 nothing lost, 1 small hassle, 2 real cost

QUESTIONS: dict[str, Any] = {
    "reversible": {
        "type": "choice",
        "instructions": "After the tool call in `action` runs, can the user or the agent fully undo it later?",
        "criteria": {
            "reversible": "It can be edited, deleted, removed or cancelled later with nothing lost. "
                          "Read-only lookups count as reversible.",
            "irreversible": "It cannot be taken back once done: a sent message, a charge, "
                            "a statement someone has already acted on.",
        },
    },
    "outward": {
        "type": "noul",
        "instructions": "Does the tool call in `action` send, submit or show anything to a person or system "
                        "other than the user's own private notes, drafts, mailbox labels or calendar?",
        "criteria": {
            "true": "Another person or an outside system receives or sees something, or the user is told "
                    "a fact they will act on.",
            "false": "Everything stays inside the user's own private workspace, or it only reads data.",
        },
    },
    "cost_if_wrong": {
        "type": "score",
        "instructions": "Suppose a detail in `action.arguments` is wrong (wrong item, wrong person, wrong status). "
                        "How bad is that for the user?",
        "criteria": [
            "Nothing is lost. The user fixes it in seconds and nobody else ever knows.",
            "A small hassle or mild embarrassment, such as cancelling a free signup.",
            "A real cost: money spent, a missed commitment, or a wrong message in front of someone who matters.",
        ],
    },
    "check": {
        "type": "choice",
        "instructions": "How much work would it take to check the details in `action.arguments` against written records?",
        "criteria": {
            "cheap": "Comparing names, titles, dates, addresses or a status against a few short documents settles it.",
            "expensive": "It needs outside investigation, a phone call, or a judgment no document can settle.",
        },
    },
}

FALLBACK_SYSTEM = (
    "You classify one tool call an AI agent wants to make. Reply with JSON only, with exactly these keys: "
    '{"reversible": true|false, "outward": true|false, "cost_if_wrong": "none"|"minor"|"real", '
    '"check": "cheap"|"expensive"}. '
    "reversible: it can be fully undone later with nothing lost. outward: another person or outside system "
    "receives or sees something, or the user is told a fact they will act on. cost_if_wrong: how bad a wrong "
    "detail would be. check: how hard it is to check the details against written records."
)


@dataclass
class Route:
    route: str                  # "pass" | "review"
    reversible: bool
    outward: bool
    stakes: str                 # "cheap" | "costly"
    check: str                  # "cheap" | "expensive" | "unknown"
    source: str                 # "jev" | "fallback_llm" | "fail_closed"
    model: str
    latency_ms: int = 0
    cost_usd: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide(reversible: bool, outward: bool, costly: bool, floor: str | None) -> str:
    """The whole routing policy, in one place. Pure code."""
    if floor == "review":
        return "review"
    if reversible and not outward and not costly:
        return "pass"
    return "review"


class Router:
    def __init__(self, models: ModelClient):
        self.models = models

    async def route(self, tool: str, args: dict[str, Any]) -> Route:
        known = tools.TOOLS.get(tool)
        floor = known.floor if known else "review"       # an unknown tool is never waved through
        state = {"action": {"tool": tool, "what_the_tool_does": tools.describe(tool), "arguments": args}}
        warnings: list[str] = []

        # 1. Jev
        try:
            reply = await self.models.systemone(state, QUESTIONS)
            a = reply.raw or {}
            p_rev = float(a["reversible"]["probabilities"]["reversible"])
            p_out = float(a["outward"]["noul"])
            score = float(a["cost_if_wrong"]["score"])
            check = str(a["check"]["choice"])
            reversible = p_rev >= P_REVERSIBLE_MIN
            outward = p_out >= P_OUTWARD_MIN
            costly = score >= COSTLY_SCORE_MIN
            return Route(
                route=decide(reversible, outward, costly, floor),
                reversible=reversible, outward=outward, stakes="costly" if costly else "cheap",
                check=check if check in ("cheap", "expensive") else "unknown",
                source="jev", model=reply.model, latency_ms=reply.latency_ms, cost_usd=reply.cost_usd,
                detail={"p_reversible": round(p_rev, 3), "p_outward": round(p_out, 3),
                        "cost_score_0_to_2": round(score, 3), "floor": floor},
            )
        except ProviderError as exc:
            warnings.append(f"router: Jev unavailable ({exc})")
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"router: Jev answer was malformed ({type(exc).__name__})")

        # 2. Cheapest fast chat model
        try:
            reply = await self.models.complete(config.ROUTER_FALLBACK_FAMILY, FALLBACK_SYSTEM,
                                               json.dumps(state, ensure_ascii=False), max_tokens=120)
            data = _loads_object(reply.text)
            reversible, outward = data["reversible"], data["outward"]
            if not isinstance(reversible, bool) or not isinstance(outward, bool):
                raise ValueError("booleans expected")
            cost = str(data["cost_if_wrong"])
            if cost not in ("none", "minor", "real"):
                raise ValueError("bad cost_if_wrong")
            check = str(data.get("check", "unknown"))
            costly = cost == "real"
            return Route(
                route=decide(reversible, outward, costly, floor),
                reversible=reversible, outward=outward, stakes="costly" if costly else "cheap",
                check=check if check in ("cheap", "expensive") else "unknown",
                source="fallback_llm", model=reply.model, latency_ms=reply.latency_ms, cost_usd=reply.cost_usd,
                detail={"cost_if_wrong": cost, "floor": floor},
                warning="; ".join(warnings),
            )
        except ProviderError as exc:
            warnings.append(f"router: fallback model unavailable ({exc})")
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"router: fallback answer was malformed ({type(exc).__name__})")

        # 3. Nobody answered. Assume the worst: it cannot be undone and a mistake is costly.
        return Route(route="review", reversible=False, outward=True, stakes="costly", check="unknown",
                     source="fail_closed", model="none", detail={"floor": floor},
                     warning="; ".join(warnings))


def _loads_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply. Raises ValueError if there is none."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON was not an object")
    return data
