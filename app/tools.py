"""Pluggable tool registry with strict read-only vs. mutating classification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    mutating: bool
    handler: Callable[[dict[str, Any]], dict[str, Any]]


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


def tool_check_service_health(args: dict[str, Any]) -> dict[str, Any]:
    target = str(args.get("service", "")).strip().lower()
    if target and target in MOCK_SERVICES:
        return {"service": target, **MOCK_SERVICES[target]}
    degraded = {k: v for k, v in MOCK_SERVICES.items() if v["status"] != "healthy"}
    return {
        "total_services": len(MOCK_SERVICES),
        "healthy_count": len(MOCK_SERVICES) - len(degraded),
        "degraded": degraded,
    }


def tool_search_kb(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).lower()
    tokens = [t for t in query.replace("?", " ").replace(",", " ").split() if len(t) > 2]
    scored = []
    for doc in SAMPLE_KB:
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
