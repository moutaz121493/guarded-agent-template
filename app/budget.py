"""Atomic SQLite cost ledger: reserves max cost before an LLM call and settles actual spend after."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path


class BudgetExceeded(Exception):
    """Raised when a reservation would breach the daily or monthly spend cap."""


class BudgetLedger:
    def __init__(self, db_path: str | Path, daily_cap_usd: float = 1.00, monthly_cap_usd: float = 10.00):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_cap_usd = float(daily_cap_usd)
        self.monthly_cap_usd = float(monthly_cap_usd)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS spend_ledger (
                    reservation_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    month TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    reserved_usd REAL NOT NULL,
                    actual_usd REAL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # one row per agent run (no task text: only what is needed to measure the service)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_log (
                    ts TEXT NOT NULL, status TEXT NOT NULL, provider TEXT NOT NULL, tool TEXT,
                    latency_ms INTEGER NOT NULL, plan_ms INTEGER, cost_usd REAL NOT NULL
                )
                """
            )

    def log_run(self, status: str, provider: str, tool: str | None, latency_ms: int, plan_ms: int | None, cost_usd: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO run_log (ts, status, provider, tool, latency_ms, plan_ms, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), status, provider, tool, int(latency_ms), plan_ms, float(cost_usd)),
            )

    def stats(self, days: int = 30) -> dict:
        """Measured numbers for the last `days` days: runs per status, p50/p95 latency, total cost."""
        since = datetime.now(timezone.utc).timestamp() - days * 86400
        with self._lock, self._connect() as conn:
            rows = [r for r in conn.execute("SELECT * FROM run_log ORDER BY ts") if datetime.fromisoformat(r["ts"]).timestamp() >= since]
        def pct(xs: list[int], q: float):
            xs = sorted(x for x in xs if x is not None)
            return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else None
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        llm = [r["plan_ms"] for r in rows if r["provider"] != "mock" and r["plan_ms"] is not None]
        return {
            "days": days, "runs": len(rows), "by_status": by_status,
            "first_run": rows[0]["ts"] if rows else None,
            "latency_ms_p50": pct([r["latency_ms"] for r in rows], .5), "latency_ms_p95": pct([r["latency_ms"] for r in rows], .95),
            "planner_ms_p50": pct(llm, .5), "planner_ms_p95": pct(llm, .95),
            "cost_usd_total": round(sum(r["cost_usd"] for r in rows), 6),
        }

    def _totals(self, conn: sqlite3.Connection, today: str, month: str) -> tuple[float, float]:
        day_sum = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0.0) FROM spend_ledger WHERE day = ? AND status != 'released'",
            (today,),
        ).fetchone()[0]
        month_sum = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0.0) FROM spend_ledger WHERE month = ? AND status != 'released'",
            (month,),
        ).fetchone()[0]
        return float(day_sum), float(month_sum)

    def summary(self) -> dict[str, float]:
        today = date.today().isoformat()
        month = today[:7]
        with self._lock, self._connect() as conn:
            day_spent, month_spent = self._totals(conn, today, month)
        day_left = max(0.0, round(self.daily_cap_usd - day_spent, 6))
        month_left = max(0.0, round(self.monthly_cap_usd - month_spent, 6))
        return {
            "daily_cap_usd": self.daily_cap_usd,
            "daily_spent_usd": round(day_spent, 6),
            "daily_remaining_usd": day_left,
            "monthly_cap_usd": self.monthly_cap_usd,
            "monthly_spent_usd": round(month_spent, 6),
            "monthly_remaining_usd": month_left,
            "budget_remaining_usd": min(day_left, month_left),
        }

    def reserve(self, max_cost_usd: float, channel: str = "api") -> str:
        if max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative")
        today = date.today().isoformat()
        month = today[:7]
        res_id = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            day_spent, month_spent = self._totals(conn, today, month)
            if day_spent + max_cost_usd > self.daily_cap_usd + 1e-9:
                raise BudgetExceeded(
                    f"Daily budget cap (${self.daily_cap_usd:.2f}) would be exceeded (spent ${day_spent:.4f})."
                )
            if month_spent + max_cost_usd > self.monthly_cap_usd + 1e-9:
                raise BudgetExceeded(
                    f"Monthly budget cap (${self.monthly_cap_usd:.2f}) would be exceeded (spent ${month_spent:.4f})."
                )
            conn.execute(
                """
                INSERT INTO spend_ledger (reservation_id, day, month, channel, reserved_usd, actual_usd, status, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, 'reserved', ?)
                """,
                (res_id, today, month, channel, float(max_cost_usd), datetime.now(timezone.utc).isoformat()),
            )
        return res_id

    def settle(self, reservation_id: str, actual_usd: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE spend_ledger SET actual_usd = ?, status = 'settled' WHERE reservation_id = ?",
                (round(max(0.0, float(actual_usd)), 6), reservation_id),
            )

    def release(self, reservation_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE spend_ledger SET actual_usd = 0.0, status = 'released' WHERE reservation_id = ?",
                (reservation_id,),
            )
