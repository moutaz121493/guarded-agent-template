"""Optional maintenance backend (MAINT_URL): real checks and fixes through an allowlisted maintenance API.

The agent never gets shell, Docker or root itself. It talks to a separate service that only knows a fixed list of
actions and targets (on the author's Pi: pi-maint). Expected API:
  GET  {MAINT_URL}/api/status     -> {"system": {...failed_units, disks, disk_errors...}, "units": [...], "containers": [...],
                                      "timers": [...], "backups": {...}, "updates": [...], "stacks": [...], "run_timers": [...]}
  POST {MAINT_URL}/api/run        {"action", "target"} -> {"id"}
  GET  {MAINT_URL}/api/job?id=ID  -> {"running": bool, "log": "..."}
Header X-Maint: 1 on every call. Actions reachable from chat are limited by CHAT_ACTIONS below (no reboot, no deleting).
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

CHAT_ACTIONS = {   # what a chat message may ask for (the maintenance service validates targets again)
    "restart-service", "restart-container", "run-timer", "backup-now", "clear-failed",   # fixes
    "apt-refresh", "apt-upgrade", "update-stack", "rollback-stack",                       # upgrades
}


def enabled() -> bool:
    return bool(os.getenv("MAINT_URL", "").strip())


def _req(path: str, body: dict | None = None, timeout: int = 30) -> Any:
    url = os.getenv("MAINT_URL", "").rstrip("/") + path
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"X-Maint": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def status() -> dict[str, Any]:
    return _req("/api/status", timeout=60)


def problems(st: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the status review into a short list of problems, each with the fix the chat may offer."""
    out: list[dict[str, Any]] = []
    sy = st.get("system", {})
    for u in sy.get("failed_units", []):
        name = u.removesuffix(".service")
        label = next((k for k, v in _timer_map(st).items() if v == name), None)
        fix = {"action": "run-timer", "target": label} if label else {"action": "clear-failed", "target": ""}
        out.append({"problem": f"systemd unit {u} failed", "fix": fix})
    for d in sy.get("disks", []):
        if not d.get("ok", True):
            out.append({"problem": f"disk {d.get('name')} not OK (mounted={d.get('mounted')}, mode={d.get('mode')}, write={d.get('write')})", "fix": None})
        elif (d.get("used_pct") or 0) >= 90:
            out.append({"problem": f"disk {d.get('name')} {d.get('used_pct')}% full", "fix": None})
    de = sy.get("disk_errors")
    n_err = de.get("count", 0) if isinstance(de, dict) else (de or 0)
    if n_err:
        out.append({"problem": f"{n_err} kernel disk errors (last: {de.get('last', '') if isinstance(de, dict) else ''})", "fix": None})
    if str(sy.get("throttled", "0x0")) not in ("0x0", ""):
        out.append({"problem": f"power/heat throttling flag {sy.get('throttled')}", "fix": None})
    for u in st.get("units", []):
        if u.get("state") not in ("active", "activating"):
            out.append({"problem": f"service {u['unit']} is {u.get('state')}", "fix": {"action": "restart-service", "target": u["unit"]}})
    for c in st.get("containers", []):
        state = str(c.get("state") or c.get("status") or "").lower()
        if state and not (state.startswith("up") or state == "running") or "unhealthy" in str(c.get("status", "")).lower():
            out.append({"problem": f"container {c.get('name')} is {c.get('status') or state}", "fix": {"action": "restart-container", "target": c.get("name")}})
    for t in st.get("timers", []):
        if t.get("result") not in ("success", "", None, "?"):
            name = next((k for k, v in _timer_map(st).items() if v == t["unit"]), None)
            out.append({"problem": f"scheduled job {t['unit']} last result: {t['result']}", "fix": {"action": "run-timer", "target": name} if name else None})
    b = st.get("backups") or {}
    for pl in (b.get("plans") or []) if isinstance(b, dict) else []:
        bad = str(pl.get("status", "")).lower() not in ("success", "running", "") or (pl.get("last_ok_h") or 0) > 30
        if bad and not pl.get("running"):
            out.append({"problem": f"backup {pl.get('label') or pl.get('plan')}: {pl.get('status')} {pl.get('message') or ''}".strip(),
                        "fix": {"action": "backup-now", "target": pl.get("label") or ""}})
    if sy.get("reboot_required"):
        out.append({"problem": "a reboot is required for installed updates (not possible from chat)", "fix": None})
    return out


# label -> systemd unit for "run now" (pi-maint's TIMERS); a backend can also send its own "timer_map"
DEFAULT_TIMER_MAP = {
    "Paperless AI (new documents)": "paperai@process", "Search index (Ask everything)": "rag-index",
    "Status refresh": "home-status", "Backup preparation (DB dumps)": "backup-prepare", "Restore test": "restore-test",
    "Disk self-test (SMART)": "smart-selftest", "Weekly report": "home-report", "Morning summary": "morning-summary",
}


def _timer_map(st: dict[str, Any]) -> dict[str, str]:
    return st.get("timer_map") or {k: v for k, v in DEFAULT_TIMER_MAP.items() if k in st.get("run_timers", [])}


def summary() -> dict[str, Any]:
    st = status()
    sy = st.get("system", {})
    return {
        "problems": problems(st)[:12],
        "updates_pending": len(st.get("updates") or []),
        "system": {"temp": sy.get("temp"), "throttled": sy.get("throttled"), "load": sy.get("load"),
                   "ram_free_mb": sy.get("mem_avail_mb"), "swap_used_mb": sy.get("swap_used_mb"), "uptime_h": sy.get("uptime_h")},
        "stacks": st.get("stacks", []), "run_timers": st.get("run_timers", []),
        "running_job": (st.get("current") or {}).get("label"),
    }


def run(action: str, target: str = "") -> dict[str, Any]:
    if action not in CHAT_ACTIONS:
        return {"started": False, "error": f"`{action}` is not allowed from chat. Allowed: {sorted(CHAT_ACTIONS)}"}
    try:
        job = _req("/api/run", {"action": action, "target": target})
    except urllib.error.HTTPError as exc:
        return {"started": False, "error": json.loads(exc.read() or b"{}").get("error", f"HTTP {exc.code}")}
    return {"started": True, "job_id": job.get("id"), "action": action, "target": target}


def job(job_id: str) -> dict[str, Any]:
    return _req(f"/api/job?id={job_id}")
