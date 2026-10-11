"""Thin HTTP clients for each model family. No SDKs: four small request shapes.

Every call returns a Reply with latency, token counts and estimated cost, or raises
ProviderError. Error text is scrubbed of any configured secret before it leaves this module.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Any

import httpx

from . import config
from .config import Endpoint


class ProviderError(Exception):
    """A model could not be reached or gave no usable answer."""


@dataclass
class Reply:
    text: str
    family: str
    provider: str
    model: str
    latency_ms: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    raw: dict[str, Any] | None = None   # structured answers (router only)

    def meta(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("text", None)
        d.pop("raw", None)
        return d


class ModelClient:
    """Calls a family by name. Tries that family's endpoints in order (failover inside a
    family is fine: the family is what matters for independence, not who serves it)."""

    def __init__(self, env: dict[str, str] | None = None, timeout: float | None = None,
                 down: set[str] | None = None):
        self.env = env if env is not None else config.load_env()
        self.timeout = timeout if timeout is not None else config.timeout_s()
        self.down = down if down is not None else config.simulated_outages()
        self._http = httpx.AsyncClient(timeout=self.timeout)
        self.calls = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ helpers
    def _scrub(self, text: str) -> str:
        for value in self.env.values():
            if value and len(value) >= 8:
                text = text.replace(value, "<redacted>")
                if value.startswith("http"):                    # also hide the bare host name
                    host = value.split("//", 1)[-1].split("/", 1)[0]
                    if host:
                        text = text.replace(host, "<redacted-host>")
        return text[:300]

    def available(self, family: str) -> bool:
        if family in self.down:
            return False
        return any(self._has_key(ep) for ep in config.ENDPOINTS.get(family, ()))

    def _has_key(self, ep: Endpoint) -> bool:
        need = {
            "google": ("GEMINI_API_KEY",),
            "anthropic": ("ANTHROPIC_API_KEY",),
            "azure": ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL"),
            "openai": ("OPENAI_API_KEY",),
            "typesafe": ("TYPESAFE_API_KEY",),
        }[ep.provider]
        return all(self.env.get(k) for k in need)

    def model_for(self, family: str) -> str:
        eps = config.ENDPOINTS.get(family, ())
        return eps[0].model if eps else "unknown"

    # ------------------------------------------------------------------ public
    async def complete(self, family: str, system: str, user: str, max_tokens: int = 400) -> Reply:
        if family in self.down:
            raise ProviderError(f"{family}: simulated outage (SO_DOWN)")
        endpoints = config.ENDPOINTS.get(family)
        if not endpoints:
            raise ProviderError(f"unknown family {family!r}")
        errors: list[str] = []
        for ep in endpoints:
            if not self._has_key(ep):
                errors.append(f"{ep.provider}: no key configured")
                continue
            try:
                return await self._call(ep, system, user, max_tokens)
            except ProviderError as exc:
                errors.append(f"{ep.provider}/{ep.model}: {exc}")
            except httpx.TimeoutException:
                errors.append(f"{ep.provider}/{ep.model}: timed out after {self.timeout:g}s")
            except httpx.HTTPError as exc:
                errors.append(f"{ep.provider}/{ep.model}: {type(exc).__name__}")
        raise ProviderError(self._scrub("; ".join(errors)))

    async def systemone(self, state: Any, questions: dict[str, Any]) -> Reply:
        """One Jev request: a state plus typed questions, typed answers back."""
        ep = config.ROUTER_ENDPOINT
        if "jev" in self.down or "typesafe" in self.down:
            raise ProviderError("jev: simulated outage (SO_DOWN)")
        if not self._has_key(ep):
            raise ProviderError("jev: no key configured")
        self.calls += 1
        t0 = time.perf_counter()
        try:
            r = await self._http.post(
                "https://api.typesafe.ai/v1/systemone",
                headers={"Authorization": f"Bearer {self.env['TYPESAFE_API_KEY']}"},
                json={"state": state, "model": ep.model, "questions": questions},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"jev: timed out after {self.timeout:g}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"jev: {type(exc).__name__}") from exc
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            raise ProviderError(self._scrub(f"jev: HTTP {r.status_code} {r.text}"))
        try:
            data = r.json()
            answers = data["answers"]
            usage = data.get("usage", {})
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("jev: malformed response") from exc
        tin, tout = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        return Reply("", "typesafe", "typesafe", str(data.get("model", ep.model)), ms, tin, tout,
                     ep.cost(tin, tout), raw=answers)

    # ------------------------------------------------------------------ wire formats
    async def _call(self, ep: Endpoint, system: str, user: str, max_tokens: int) -> Reply:
        self.calls += 1
        t0 = time.perf_counter()
        if ep.kind == "gemini":
            r = await self._http.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{ep.model}:generateContent",
                headers={"x-goog-api-key": self.env["GEMINI_API_KEY"]},
                json={
                    "system_instruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"responseMimeType": "application/json",
                                         "maxOutputTokens": max_tokens, "temperature": 0},
                },
            )
            data = self._json(r)
            try:
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError("no text in response") from exc
            um = data.get("usageMetadata", {})
            tin = int(um.get("promptTokenCount", 0))
            tout = int(um.get("candidatesTokenCount", 0)) + int(um.get("thoughtsTokenCount", 0))
            model = str(data.get("modelVersion", ep.model))
        elif ep.kind == "anthropic":
            headers = {"x-api-key": self.env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"}
            if self.env.get("ANTHROPIC_WORKSPACE_ID"):
                headers["anthropic-workspace-id"] = self.env["ANTHROPIC_WORKSPACE_ID"]
            r = await self._http.post(
                "https://api.anthropic.com/v1/messages", headers=headers,
                json={"model": ep.model, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]},
            )
            data = self._json(r)
            try:
                text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            except (KeyError, TypeError) as exc:
                raise ProviderError("no text in response") from exc
            usage = data.get("usage", {})
            tin, tout = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
            model = str(data.get("model", ep.model))
        elif ep.kind == "openai_chat":
            if ep.provider == "azure":
                base = self.env["AZURE_OPENAI_BASE_URL"].rstrip("/")
                headers = {"api-key": self.env["AZURE_OPENAI_API_KEY"]}
            else:
                base = "https://api.openai.com/v1"
                headers = {"Authorization": f"Bearer {self.env['OPENAI_API_KEY']}"}
            r = await self._http.post(
                f"{base}/chat/completions", headers=headers,
                json={"model": ep.model,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}],
                      # Small GPT models spend some of this on hidden reasoning tokens.
                      "max_completion_tokens": max(max_tokens * 3, 1200),
                      "response_format": {"type": "json_object"}},
            )
            data = self._json(r)
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError("no text in response") from exc
            usage = data.get("usage", {})
            tin, tout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
            model = str(data.get("model", ep.model))
        else:
            raise ProviderError(f"unknown wire format {ep.kind!r}")
        if not text.strip():
            raise ProviderError("empty response")
        ms = int((time.perf_counter() - t0) * 1000)
        return Reply(text, ep.family, ep.provider, model, ms, tin, tout, ep.cost(tin, tout))

    def _json(self, r: httpx.Response) -> dict[str, Any]:
        if r.status_code != 200:
            raise ProviderError(self._scrub(f"HTTP {r.status_code} {r.text}"))
        try:
            data = r.json()
        except ValueError as exc:
            raise ProviderError("response was not JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("response was not a JSON object")
        return data
