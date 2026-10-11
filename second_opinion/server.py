"""The demo server. Serves the page and runs actions through the gateway with real model calls.

    uvicorn second_opinion.server:app --port 8011

All tools are simulated. The only things that leave this machine are the model calls.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .evalrun import load_cases
from .gateway import Action, Gateway
from .providers import ModelClient
from .records import RecordStore

DEMO_IDS = ("p01", "c01", "e01")     # passes, agrees, is caught
state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    models = ModelClient()
    state["models"] = models
    state["cases"] = {c["id"]: c for c in load_cases()}
    state["gateway"] = Gateway(models, RecordStore(config.ROOT / "records" / "decisions.jsonl"), mode="cross")
    yield
    await models.aclose()


app = FastAPI(title="Second Opinion", lifespan=lifespan)


class Ruling(BaseModel):
    approve: bool


def public_case(c: dict[str, Any], models: ModelClient) -> dict[str, Any]:
    return {"id": c["id"], "tool": c["tool"], "args": c["args"], "task": c["task"], "evidence": c["evidence"],
            "proposer_family": c["proposer_family"], "proposer_model": models.model_for(c["proposer_family"]),
            "rationale": c["rationale"]}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    models: ModelClient = state["models"]
    return {"ok": True, "live": True,
            "families": {f: {"available": models.available(f), "model": models.model_for(f)} for f in config.FAMILIES},
            "router": {"model": config.ROUTER_ENDPOINT.model, "key_present": bool(models.env.get("TYPESAFE_API_KEY")),
                       "simulated_down": "jev" in models.down},
            "simulated_outages": sorted(models.down), "tools": "simulated"}


@app.get("/api/demo")
async def demo() -> dict[str, Any]:
    return {"cases": [public_case(state["cases"][i], state["models"]) for i in DEMO_IDS]}


@app.post("/api/run/{case_id}")
async def run_case(case_id: str) -> StreamingResponse:
    case = state["cases"].get(case_id)
    if case is None:
        raise HTTPException(404, f"no case {case_id}")
    gateway: Gateway = state["gateway"]
    action = Action.from_dict({**case, "proposer_model": state["models"].model_for(case["proposer_family"])})
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def work() -> None:
        try:
            await gateway.submit(action, emit=queue.put)
        except Exception as exc:        # e.g. the record could not be written: say so, loudly
            await queue.put({"type": "error", "action_id": case_id,
                             "text": f"{type(exc).__name__}: {str(exc)[:200]}. The action did not run."})
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(work())
        try:
            while (event := await queue.get()) is not None:
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            await task

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/resolve/{decision_id}")
async def resolve(decision_id: str, ruling: Ruling) -> JSONResponse:
    gateway: Gateway = state["gateway"]
    events: list[dict[str, Any]] = []

    async def emit(e: dict[str, Any]) -> None:
        events.append(e)

    try:
        await gateway.resolve(decision_id, ruling.approve, emit=emit)
    except KeyError:
        raise HTTPException(404, "no card is waiting under that id (already ruled on, or the server restarted)")
    return JSONResponse({"events": events})


@app.get("/api/records")
async def records() -> dict[str, Any]:
    gateway: Gateway = state["gateway"]
    return {"records": list(gateway.records.recent)[-20:], "written": gateway.records.written,
            "pending_cards": len(gateway.pending)}


app.mount("/", StaticFiles(directory=str(config.ROOT / "docs"), html=True), name="page")
