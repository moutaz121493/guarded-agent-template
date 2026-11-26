"""FastAPI entrypoint for guarded-agent-template."""
from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .agent import GuardedAgent
from .budget import BudgetLedger

DB_PATH = Path(os.getenv("BUDGET_DB_PATH", "/tmp/guarded-agent-budget.db"))
DAILY_CAP = float(os.getenv("DAILY_BUDGET_USD", "1.00"))
MONTHLY_CAP = float(os.getenv("MONTHLY_BUDGET_USD", "10.00"))

ledger = BudgetLedger(DB_PATH, daily_cap_usd=DAILY_CAP, monthly_cap_usd=MONTHLY_CAP)
agent = GuardedAgent(ledger=ledger)

app = FastAPI(title="guarded-agent-template", version="1.0.0")


class RunRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=2000)
    channel: str = Field(default="api", max_length=64)
    max_steps: int = Field(default=3, ge=1, le=8)
    max_cost_usd: float = Field(default=0.02, ge=0.0, le=1.0)


class ApprovalRequest(BaseModel):
    code: str = Field(..., min_length=2, max_length=16)
    approved: bool = True


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "provider": agent.provider, **ledger.summary()}


@app.post("/v1/run")
def run_agent(req: RunRequest) -> dict:
    res = agent.run(
        task=req.task,
        channel=req.channel,
        max_steps=req.max_steps,
        max_cost_usd=req.max_cost_usd,
    )
    return asdict(res)


@app.post("/v1/approve")
def approve_action(req: ApprovalRequest) -> dict:
    return agent.approve(code=req.code, approved=req.approved)


@app.get("/v1/stats")
def run_stats(days: int = 30) -> dict:
    return {"provider": agent.provider, "last_planner_error": agent.last_planner_error, **ledger.stats(max(1, min(days, 365)))}


@app.get("/v1/traces")
def list_traces() -> dict:
    return {"count": len(agent.recent_traces), "traces": agent.recent_traces}
