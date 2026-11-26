#!/usr/bin/env python3
"""Run golden evaluation suite and emit a Markdown scorecard."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import GuardedAgent  # noqa: E402
from app.budget import BudgetLedger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    args = parser.parse_args()

    dataset_path = ROOT / "evals" / "dataset.jsonl"
    cases = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    with tempfile.TemporaryDirectory() as td:
        ledger = BudgetLedger(pathlib.Path(td) / "eval.db", daily_cap_usd=5.0, monthly_cap_usd=50.0)
        ag = GuardedAgent(ledger=ledger, provider=args.provider)

        passed = 0
        total_cost = 0.0
        total_ms = 0
        rows: list[str] = []

        for c in cases:
            res = ag.run(c["task"], channel="eval")
            chosen_tool = res.pending_tool or next((s.get("tool") for s in res.trace if s.get("action") == "plan"), None)
            ok = (res.status == c["expect_status"]) and (chosen_tool == c["expect_tool"])
            if ok:
                passed += 1
            total_cost += res.cost_usd
            total_ms += res.latency_ms
            rows.append(
                f"| `{c['id']}` | `{res.status}` | `{chosen_tool}` | {res.latency_ms} ms | ${res.cost_usd:.4f} | {'PASS' if ok else 'FAIL'} |"
            )

    rate = passed / len(cases)
    print(f"## Guarded Agent Eval Scorecard (`provider={args.provider}`)\n")
    print(f"- **Pass rate:** {passed}/{len(cases)} ({rate * 100:.1f}%)")
    print(f"- **Avg latency:** {total_ms / len(cases):.1f} ms")
    print(f"- **Total cost:** ${total_cost:.4f}\n")
    print("| Case | Status | Tool | Latency | Cost | Result |")
    print("| :--- | :--- | :--- | ---: | ---: | :---: |")
    print("\n".join(rows))

    return 0 if rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    raise SystemExit(main())
