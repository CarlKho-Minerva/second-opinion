"""Run the same 30 actions through four conditions and report what happened.

    A  none        no review
    B  self        the proposer's own model re-checks, with its reasoning in view
    B' self_fresh  the proposer's own model re-checks in a fresh context (control)
    C  cross       a different family gives the second opinion (the product)

The router runs once per action and its answer is shared by B, B' and C, so the three are
compared on identical routing. Real calls. Results go to data/results.json and docs/results.js.

    python -m second_opinion.evalrun            # full run
    python -m second_opinion.evalrun --routes   # router only, no reviewers
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .gateway import Action, Gateway
from .providers import ModelClient
from .records import RecordStore
from .router import Route, Router

CONDITIONS = [("A", "none", "No review"),
              ("B", "self", "Same model checks itself (sees its own reasoning)"),
              ("B'", "self_fresh", "Same model, fresh context (control)"),
              ("C", "cross", "Second opinion from a different family")]
CONCURRENCY = 4


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or config.ROOT / "data" / "cases.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def pct(n: int, d: int) -> str:
    return f"{n}/{d}"


def summarise(label: str, mode: str, name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    errs = [r for r in rows if r["has_error"]]
    oks = [r for r in rows if not r["has_error"]]
    stopped = lambda r: not r["executed"]                       # noqa: E731
    carded = lambda r: r["outcome"] == "pending_human"          # noqa: E731
    lat = [r["added_latency_ms"] for r in rows]
    reviewed = [r["added_latency_ms"] for r in rows if r["opinions"]]
    return {
        "label": label, "mode": mode, "name": name, "n": len(rows),
        "errors_total": len(errs),
        "errors_caught": sum(1 for r in errs if stopped(r)),
        "errors_caught_auto": sum(1 for r in errs if stopped(r) and not carded(r)),
        "errors_caught_by_card": sum(1 for r in errs if carded(r)),
        "errors_executed": sum(1 for r in errs if r["executed"]),
        "correct_total": len(oks),
        "correct_wrongly_blocked": sum(1 for r in oks if stopped(r) and not carded(r)),
        "correct_sent_to_human": sum(1 for r in oks if carded(r)),
        "human_cards": sum(1 for r in rows if carded(r)),
        "latency_ms_mean": round(statistics.fmean(lat)) if lat else 0,
        "latency_ms_median": round(statistics.median(lat)) if lat else 0,
        "latency_ms_max": max(lat) if lat else 0,
        "latency_ms_mean_reviewed": round(statistics.fmean(reviewed)) if reviewed else 0,
        "cost_usd_total": round(sum(r["cost_usd"] for r in rows), 6),
        "cost_usd_per_action": round(sum(r["cost_usd"] for r in rows) / max(len(rows), 1), 6),
        "model_calls": sum(len(r["opinions"]) + len(r["attempts"]) for r in rows),
        "unparsed_or_failed_calls": sum(len(r["attempts"]) for r in rows),
    }


def markdown_table(summaries: list[dict[str, Any]]) -> str:
    head = ("| Condition | Planted errors caught | of which needed a person | Correct actions wrongly blocked | "
            "Correct actions sent to a person | Human cards | Added latency, mean (ms) | Added cost, 30 actions (USD) |\n"
            "|---|---|---|---|---|---|---|---|\n")
    body = ""
    for s in summaries:
        body += (f"| {s['label']}. {s['name']} | {pct(s['errors_caught'], s['errors_total'])} | "
                 f"{s['errors_caught_by_card']} | {pct(s['correct_wrongly_blocked'], s['correct_total'])} | "
                 f"{pct(s['correct_sent_to_human'], s['correct_total'])} | {s['human_cards']} | "
                 f"{s['latency_ms_mean']} | {s['cost_usd_total']:.4f} |\n")
    return head + body


async def route_all(models: ModelClient, cases: list[dict[str, Any]]) -> dict[str, Route]:
    router = Router(models)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(c: dict[str, Any]) -> tuple[str, Route]:
        async with sem:
            return c["id"], await router.route(c["tool"], c["args"])

    return dict(await asyncio.gather(*(one(c) for c in cases)))


async def run_condition(models: ModelClient, cases: list[dict[str, Any]], routes: dict[str, Route],
                        mode: str) -> list[dict[str, Any]]:
    gateway = Gateway(models, RecordStore(None), mode=mode)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(c: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            route = routes[c["id"]]
            rec = await gateway.submit(Action.from_dict(c), route=route)
            router_ms = 0 if mode == "none" else route.latency_ms
            return {
                "id": c["id"], "kind": c["kind"], "has_error": c["has_error"], "tool": c["tool"],
                "proposer_family": c["proposer_family"],
                "route": None if mode == "none" else route.route,
                "stakes": None if mode == "none" else route.stakes,
                "outcome": rec["outcome"], "executed": rec["executed"],
                "opinions": [{k: o[k] for k in ("role", "family", "model", "verdict", "reason", "latency_ms", "cost_usd")}
                             for o in rec["opinions"]],
                "attempts": rec["attempts"], "warnings": rec["warnings"],
                "added_latency_ms": rec["added_latency_ms"] + router_ms,
                "cost_usd": rec["cost_usd"],
            }

    return list(await asyncio.gather(*(one(c) for c in cases)))


async def main_async(routes_only: bool) -> int:
    cases = load_cases()
    models = ModelClient()
    missing = [f for f in config.FAMILIES if not models.available(f)]
    if missing:
        print(f"FAILED: no usable key for {', '.join(missing)}. Nothing was run.", file=sys.stderr)
        return 2
    try:
        routes = await route_all(models, cases)
        router_rows = [{"id": c["id"], "tool": c["tool"], "expected_route": c["expected_route"],
                        **routes[c["id"]].as_dict()} for c in cases]
        agree = sum(1 for r in router_rows if r["route"] == r["expected_route"])
        for r in router_rows:
            flag = "" if r["route"] == r["expected_route"] else "   <-- differs from hand label"
            print(f"{r['id']} {r['tool']:<18} {r['route']:<6} rev={str(r['reversible']):<5} out={str(r['outward']):<5} "
                  f"{r['stakes']:<6} check={r['check']:<9} {r['source']} {r['latency_ms']}ms {r['detail']}{flag}")
        print(f"router agrees with hand labels on {agree}/{len(cases)}")
        if routes_only:
            return 0

        summaries, per_case = [], {}
        for label, mode, name in CONDITIONS:
            rows = await run_condition(models, cases, routes, mode)
            per_case[label] = rows
            summaries.append(summarise(label, mode, name, rows))
            print(f"done: {label} ({mode})", file=sys.stderr)

        out = {
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cases": len(cases), "seed": 1011,
            "models": {f: config.ENDPOINTS[f][0].model + " via " + config.ENDPOINTS[f][0].provider for f in config.FAMILIES},
            "router": {"sources": sorted({r["source"] for r in router_rows}),
                       "models": sorted({r["model"] for r in router_rows}),
                       "agrees_with_hand_labels": f"{agree}/{len(cases)}",
                       "latency_ms_mean": round(statistics.fmean(r["latency_ms"] for r in router_rows)),
                       "cost_usd_total": round(sum(r["cost_usd"] for r in router_rows), 6),
                       "rows": router_rows},
            "total_model_calls": models.calls,
            "summaries": summaries, "per_case": per_case,
            "markdown": markdown_table(summaries),
        }
        (config.ROOT / "data" / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        slim = {k: v for k, v in out.items() if k != "markdown"}
        (config.ROOT / "docs" / "results.js").write_text(
            "window.SO_RESULTS = " + json.dumps(slim, ensure_ascii=False) + ";\n", encoding="utf-8")
        print()
        print(out["markdown"])
        print(f"total model calls this run: {models.calls}")
        return 0
    finally:
        await models.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routes", action="store_true", help="router only")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args.routes)))


if __name__ == "__main__":
    main()
