"""Settings: where keys come from, which model stands for each family, what a token costs.

Keys are read from the process environment first, then from a local .env, then from the
file named by SO_ENV_FILE. They are held in memory only. Nothing here prints or logs one.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A "family" is who trained the model. A "provider" is who serves it. Azure-hosted GPT is
# the openai family, so it can never be the second opinion for an OpenAI proposal.
FAMILIES = ("google", "anthropic", "openai")

# Several names can hold the same secret. The first one that is set wins.
KEY_ALIASES = {
    "TYPESAFE_API_KEY": ("TYPESAFE_API_KEY",),
    "GEMINI_API_KEY": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "ANTHROPIC_API_KEY": ("ANTHROPIC_API_KEY",),
    "ANTHROPIC_WORKSPACE_ID": ("ANTHROPIC_WORKSPACE_ID",),
    "AZURE_OPENAI_API_KEY": ("AZURE_OPENAI_API_KEY",),
    "AZURE_OPENAI_BASE_URL": ("AZURE_OPENAI_BASE_URL",),
    "OPENAI_API_KEY": ("OPENAI_API_KEY",),
}


@dataclass(frozen=True)
class Endpoint:
    family: str          # google | anthropic | openai | typesafe
    provider: str        # who serves it: google | anthropic | azure | openai | typesafe
    model: str
    kind: str            # wire format: gemini | anthropic | openai_chat | systemone
    usd_per_mtok_in: float
    usd_per_mtok_out: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.usd_per_mtok_in + tokens_out * self.usd_per_mtok_out) / 1_000_000


# Prices are vendor list prices in USD per million tokens, typed in by hand on 2026-10-11.
# Sources are linked in the README. They are estimates of spend, not an invoice.
ENDPOINTS: dict[str, tuple[Endpoint, ...]] = {
    "google": (
        Endpoint("google", "google", "gemini-3.5-flash-lite", "gemini", 0.30, 2.50),
    ),
    "anthropic": (
        Endpoint("anthropic", "anthropic", "claude-haiku-4-5", "anthropic", 1.00, 5.00),
    ),
    "openai": (
        Endpoint("openai", "azure", "gpt-6-luna", "openai_chat", 0.10, 0.50),
        Endpoint("openai", "openai", "gpt-5.4-nano", "openai_chat", 0.20, 1.25),
    ),
}

ROUTER_ENDPOINT = Endpoint("typesafe", "typesafe", "jev-latest", "systemone", 0.042, 0.0)
# If Jev cannot be reached the router falls back to the cheapest fast model we have.
ROUTER_FALLBACK_FAMILY = "google"


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[m.group(1)] = value
    return out


def load_env() -> dict[str, str]:
    """Return the canonical key names that could be resolved. Missing ones are simply absent."""
    sources: list[dict[str, str]] = [dict(os.environ)]
    sources.append(_parse_env_file(ROOT / ".env"))
    extra = os.environ.get("SO_ENV_FILE")
    if extra:
        sources.append(_parse_env_file(Path(extra).expanduser()))
    # SO_KEY_ALIASES="ANTHROPIC_API_KEY=MY_OTHER_NAME,..." lets a shared env file use its own names.
    extra_aliases: dict[str, tuple[str, ...]] = {}
    for pair in os.environ.get("SO_KEY_ALIASES", "").split(","):
        canonical, _, other = pair.strip().partition("=")
        if canonical in KEY_ALIASES and other:
            extra_aliases[canonical] = extra_aliases.get(canonical, ()) + (other.strip(),)
    resolved: dict[str, str] = {}
    for canonical, base in KEY_ALIASES.items():
        aliases = base + extra_aliases.get(canonical, ())
        for src in sources:
            hit = next((src[a] for a in aliases if src.get(a)), None)
            if hit:
                resolved[canonical] = hit
                break
    return resolved


def timeout_s() -> float:
    try:
        return float(os.environ.get("SO_TIMEOUT_S", "20"))
    except ValueError:
        return 20.0


def simulated_outages() -> set[str]:
    """SO_DOWN=anthropic,jev pretends those are unreachable, to rehearse failure paths."""
    return {x.strip().lower() for x in os.environ.get("SO_DOWN", "").split(",") if x.strip()}
