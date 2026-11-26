"""Pluggable tool registry with strict read-only vs. mutating classification."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    mutating: bool
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    admin: bool = False   # needs ADMIN_KEYWORD in the message on chat channels (see agent.py)
    # optional: turn loose model args into exact ones BEFORE asking for approval -> (args, error, human description)
    prepare: Callable[[dict[str, Any]], tuple[dict[str, Any], str | None, str]] | None = None


SAMPLE_KB = [
    {
        "id": "doc-01",
        "title": "Homelab 3-2-1 Backup Policy",
        "content": "Encrypted restic backups run nightly at 02:30 CET to local NVMe/USB, Google Drive, and Cloudflare R2. Monthly restore checksums verify integrity.",
    },
    {
        "id": "doc-02",
        "title": "Example Internet Contract (fictional)",
        "content": "ExampleNet fibre internet costs 39.99 EUR per month, invoiced on the 1st. Notice period is 1 month after the initial term. (Fictional sample data.)",
    },
    {
        "id": "doc-03",
        "title": "Example Car Insurance (fictional)",
        "content": "Sample Insurance Co. car insurance premium is 600.00 EUR/year (50.00 EUR/month). Cancellation deadline is one month before renewal. (Fictional sample data.)",
    },
    {
        "id": "doc-04",
        "title": "CAN FD Bit Timing Reference",
        "content": "Nominal arbitration phase runs at 500 kbit/s; data phase switches to 2 Mbit/s with Transmitter Delay Compensation (TDC) enabled.",
    },
]

MOCK_SERVICES = {
    "n8n": {"status": "healthy", "memory_mb": 218, "uptime_hours": 142},
    "paperless": {"status": "healthy", "memory_mb": 410, "uptime_hours": 310},
    "vaultwarden": {"status": "healthy", "memory_mb": 42, "uptime_hours": 720},
    "uptime-kuma": {"status": "healthy", "memory_mb": 94, "uptime_hours": 720},
    "worker-queue": {"status": "degraded", "memory_mb": 376, "uptime_hours": 4, "note": "high queue latency"},
}

NOTES_STORE: list[dict[str, str]] = []


def _live_snapshot() -> dict[str, Any] | None:
    """Real health from a read-only snapshot directory (HEALTH_SNAPSHOT_DIR), e.g. files written by Uptime Kuma
    exporters on the host: services.json ({"groups": [{"items": [{"name", "status" 1/0, "ping"}]}]}) and optional
    status.json ({"sys": {...}, "backup_state": ...}). The agent never talks to Docker itself."""
    root = os.getenv("HEALTH_SNAPSHOT_DIR", "").strip()
    if not root:
        return None
    try:
        services = json.loads((Path(root) / "services.json").read_text())
    except (OSError, ValueError):
        return None
    items = [it for g in services.get("groups", []) for it in g.get("items", [])]
    system: dict[str, Any] = {}
    try:
        st = json.loads(Path(os.getenv("HEALTH_STATUS_PATH") or Path(root) / "status.json").read_text())
        sy = st.get("sys", {})
        system = {"temp_c": sy.get("temp_c"), "ram_pct": sy.get("mem_pct"), "load": sy.get("load"), "last_backup": st.get("backup_state")}
    except (OSError, ValueError):
        pass
    return {"items": items, "system": system}


def tool_check_service_health(args: dict[str, Any]) -> dict[str, Any]:
    target = str(args.get("service", "")).strip().lower()
    live = _live_snapshot()
    if live is not None:
        items = live["items"]
        if target:
            norm = lambda x: "".join(ch for ch in x.lower() if ch.isalnum())   # "Vault Warden" (speech) == "Vaultwarden"
            t = norm(target)
            hit = next((it for it in items if t and (t in norm(it["name"]) or norm(it["name"]) in t)), None)
            if not hit:
                return {"service": target, "status": "unknown", "system": live["system"]}
            return {"service": hit["name"], "status": "healthy" if hit.get("status") == 1 else "down", "ping_ms": hit.get("ping"), "system": live["system"]}
        down = {it["name"]: {"status": "down"} for it in items if it.get("status") != 1}
        return {"total_services": len(items), "healthy_count": len(items) - len(down), "degraded": down, "system": live["system"]}
    if target and target in MOCK_SERVICES:
        return {"service": target, **MOCK_SERVICES[target]}
    degraded = {k: v for k, v in MOCK_SERVICES.items() if v["status"] != "healthy"}
    return {
        "total_services": len(MOCK_SERVICES),
        "healthy_count": len(MOCK_SERVICES) - len(degraded),
        "degraded": degraded,
    }


def _kb() -> list[dict[str, str]]:
    """KB_PATH = a JSON list of {"id", "title", "content"} for real deployments; otherwise the sample documents."""
    path = os.getenv("KB_PATH", "").strip()
    if path:
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return []
    return SAMPLE_KB


def tool_search_kb(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).lower()
    tokens = [t for t in query.replace("?", " ").replace(",", " ").split() if len(t) > 2]
    scored = []
    for doc in _kb():
        hay = (doc["title"] + " " + doc["content"]).lower()
        hits = sum(1 for tok in tokens if tok in hay)
        if hits > 0:
            scored.append((hits, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    matches = [d for _, d in scored[:2]]
    return {"query": query, "matches": matches}


def tool_draft_note(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text", "")).strip()[:400]
    entry = {"id": f"note-{len(NOTES_STORE) + 1:03d}", "text": text}
    NOTES_STORE.append(entry)
    return {"saved": True, "note": entry}


def tool_restart_service(args: dict[str, Any]) -> dict[str, Any]:
    service = str(args.get("service", "")).strip().lower()
    if os.getenv("RESTART_MODE", "mock").strip().lower() == "dry-run":
        # live deployments: the approval flow is real, the restart is not (the agent has no Docker access on purpose)
        return {"restarted": False, "dry_run": True, "service": service,
                "note": f"Approved. Dry-run deployment: `{service}` was NOT restarted; this agent has no access to Docker."}
    if service not in MOCK_SERVICES:
        return {"restarted": False, "error": f"Unknown service '{service}'. Allowed: {sorted(MOCK_SERVICES)}"}
    MOCK_SERVICES[service]["status"] = "healthy"
    MOCK_SERVICES[service]["note"] = "restarted after human approval"
    return {"restarted": True, "service": service, "new_status": "healthy"}


REGISTRY: dict[str, ToolSpec] = {
    "check_service_health": ToolSpec(
        name="check_service_health",
        description="Read-only health check for homelab Docker containers.",
        mutating=False,
        handler=tool_check_service_health,
    ),
    "search_kb": ToolSpec(
        name="search_kb",
        description="Read-only search over the local knowledge base (backups, contracts, CAN FD docs).",
        mutating=False,
        handler=tool_search_kb,
    ),
    "draft_note": ToolSpec(
        name="draft_note",
        description="Save a short note or reminder locally.",
        mutating=False,
        handler=tool_draft_note,
    ),
    "restart_service": ToolSpec(
        name="restart_service",
        description="Restart a whitelisted container. Mutating action — always requires explicit human approval.",
        mutating=True,
        handler=tool_restart_service,
    ),
}


# ---------- optional real maintenance tools (MAINT_URL set): checks and fixes through an allowlisted API ----------
from . import maint  # noqa: E402


def tool_check_errors(args: dict[str, Any]) -> dict[str, Any]:
    return maint.summary()


def tool_fix(args: dict[str, Any]) -> dict[str, Any]:
    return maint.run(str(args.get("action", "")), str(args.get("target", "") or ""))


def tool_upgrade(args: dict[str, Any]) -> dict[str, Any]:
    kind = str(args.get("kind", "system")).lower()
    target = str(args.get("target", "") or "")
    if kind == "stack" and target:
        return maint.run("update-stack", target)
    if kind == "rollback" and target:
        return maint.run("rollback-stack", target)
    return maint.run("apt-upgrade", "")


def tool_job_status(args: dict[str, Any]) -> dict[str, Any]:
    st = maint.status()
    return {"running": st.get("current"), "last_jobs": (st.get("history") or [])[:3]}


def _norm(x: str) -> str:
    return "".join(ch for ch in str(x).lower() if ch.isalnum())


def _best(want: str, names: list[str]) -> str | None:
    """Exact-ish match of a spoken/loose name against known names (substring either way, then word overlap)."""
    w = _norm(want)
    if not w:
        return None
    for n in names:
        if w == _norm(n):
            return n
    hits = [n for n in names if w in _norm(n) or _norm(n) in w]
    if len(hits) == 1:
        return hits[0]
    words = {t for t in str(want).lower().replace("-", " ").split() if len(t) > 2 and t not in ("job", "the", "run", "now", "service", "container")}
    scored = sorted(((len(words & set(n.lower().replace("(", " ").replace(")", " ").replace("-", " ").split())), n) for n in names), reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]) else None


def _targets(st: dict[str, Any]) -> dict[str, list[str]]:
    return {"restart-service": [u["unit"] for u in st.get("units", [])], "restart-container": [c["name"] for c in st.get("containers", [])],
            "run-timer": list(st.get("run_timers", [])), "backup-now": list(st.get("plans", [])),
            "update-stack": list(st.get("stacks", [])), "rollback-stack": list(st.get("stacks", []))}


LABELS = {"restart-service": "Restart the service", "restart-container": "Restart the container", "run-timer": "Run the job now",
          "backup-now": "Back up now", "clear-failed": "Clear the failed-services list", "apt-upgrade": "Install all system updates",
          "apt-refresh": "Check for system updates", "update-stack": "Update the app stack", "rollback-stack": "Roll back the app stack"}


def prepare_fix(args: dict[str, Any]) -> tuple[dict[str, Any], str | None, str]:
    action, target = str(args.get("action", "")), str(args.get("target", "") or "")
    if action not in maint.CHAT_ACTIONS:
        return args, f"I can't do `{action}` from chat. I can: " + ", ".join(sorted(LABELS[a] for a in maint.CHAT_ACTIONS if a in LABELS)) + ".", ""
    names = _targets(maint.status()).get(action)
    if names is not None and action != "backup-now":
        exact = _best(target, names)
        if not exact:
            return args, f"Which one? Options: {', '.join(names)}", ""
        target = exact
    elif action == "backup-now":
        target = _best(target, names or []) or ""   # empty = all backup plans
    desc = f"{LABELS.get(action, action)}{' ' + chr(96) + target + chr(96) if target else ''}"
    return {"action": action, "target": target}, None, desc


def prepare_upgrade(args: dict[str, Any]) -> tuple[dict[str, Any], str | None, str]:
    kind = str(args.get("kind", "system")).lower()
    if kind in ("stack", "rollback"):
        stacks = _targets(maint.status())["update-stack"]
        exact = _best(str(args.get("target", "")), stacks)
        if not exact:
            return args, f"Which app stack? Options: {', '.join(stacks)}", ""
        return {"kind": kind, "target": exact}, None, f"{LABELS['update-stack' if kind == 'stack' else 'rollback-stack']} `{exact}`"
    return {"kind": "system"}, None, LABELS["apt-upgrade"] + " (services may restart briefly)"


def prepare_restart(args: dict[str, Any]) -> tuple[dict[str, Any], str | None, str]:
    st = _targets(maint.status())
    want = str(args.get("service", ""))
    unit = _best(want, st["restart-service"])
    if unit:
        return {"service": unit}, None, f"{LABELS['restart-service']} `{unit}`"
    cont = _best(want, st["restart-container"])
    if cont:
        return {"service": cont}, None, f"{LABELS['restart-container']} `{cont}`"
    several = [n for n in st["restart-service"] + st["restart-container"] if _norm(want) and _norm(want) in _norm(n)]
    if several:
        return args, f"Which one? {', '.join(several)}", ""
    return args, f"No service or container matches `{want}`.", ""


def tool_restart_real(args: dict[str, Any]) -> dict[str, Any]:
    """Restart via the maintenance API: a known systemd unit or an existing container (fuzzy name match)."""
    want = "".join(ch for ch in str(args.get("service", "")).lower() if ch.isalnum())
    st = maint.status()
    norm = lambda x: "".join(ch for ch in x.lower() if ch.isalnum())
    unit = next((u["unit"] for u in st.get("units", []) if want and (want in norm(u["unit"]) or norm(u["unit"]) in want)), None)
    if unit:
        return maint.run("restart-service", unit)
    cont = next((c["name"] for c in st.get("containers", []) if want and (want in norm(c["name"]) or norm(c["name"]) in want)), None)
    if cont:
        return maint.run("restart-container", cont)
    return {"started": False, "error": f"No service or container matches `{args.get('service')}`."}


def register_maint_tools() -> None:
    REGISTRY["check_service_health"] = ToolSpec(REGISTRY["check_service_health"].name, REGISTRY["check_service_health"].description,
                                                False, REGISTRY["check_service_health"].handler, admin=True)
    REGISTRY["check_errors"] = ToolSpec("check_errors", "Deep check: errors, failed services/timers, stopped containers, disk errors, backups AND whether system updates are pending.", False, tool_check_errors, admin=True)
    REGISTRY["fix"] = ToolSpec("fix", "Run one fix: restart-service, restart-container, run-timer (run a scheduled job now), backup-now or clear-failed (args action, target).", True, tool_fix, admin=True, prepare=prepare_fix)
    REGISTRY["upgrade"] = ToolSpec("upgrade", "INSTALL system updates (kind=system), update one app stack (kind=stack, target) or roll one back (kind=rollback, target). Only when asked to install/upgrade, not to check.", True, tool_upgrade, admin=True, prepare=prepare_upgrade)
    REGISTRY["job_status"] = ToolSpec("job_status", "Status of the running/last maintenance jobs.", False, tool_job_status, admin=True)
    REGISTRY["restart_service"] = ToolSpec("restart_service", "Restart a service or container by name. Mutating — needs approval.", True, tool_restart_real, admin=True, prepare=prepare_restart)


if maint.enabled():
    register_maint_tools()
