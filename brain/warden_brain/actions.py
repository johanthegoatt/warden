"""Policy: which action, at which risk tier, and how it runs.

low     re-probe, collect diagnostics, restart a service marked stateless
medium  restart anything else; waits for an approval from the dashboard
high    rollback; written as a recommendation with a plan, never executed
"""

from __future__ import annotations

import json
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

READ_ONLY = {"none", "collect_diagnostics"}


def plan(verdict: dict, hypothesis: Optional[dict], target) -> dict:
    v = verdict["verdict"]
    if v == "healthy":
        return {"action": "none", "tier": None, "why": "nothing is wrong"}
    if verdict["location"] == "probe":
        return {
            "action": "collect_diagnostics",
            "tier": "low",
            "why": f"the fault is in Warden's own probe path, so {target.name} is left alone",
        }
    if verdict["location"] == "dependency":
        return {
            "action": "collect_diagnostics",
            "tier": "low",
            "why": (
                f"{verdict['culprit']} is the cause; restarting {target.name} would not help. "
                f"{verdict['culprit']} gets its own incident if Warden watches it."
            ),
        }
    if v == "degrading":
        return {
            "action": "collect_diagnostics",
            "tier": "low",
            "why": "the service still answers; capture its state while it degrades and keep watching",
        }
    if target.restart:
        return {
            "action": "restart",
            "tier": "low" if target.stateless else "medium",
            "spec": target.restart,
            "why": (
                f"{target.name} is marked stateless, so a restart loses nothing"
                if target.stateless
                else f"{target.name} may hold state, so a restart needs approval"
            ),
        }
    rollback = target.rollback or f"redeploy the last version of {target.name} that passed its probes"
    return {
        "action": "rollback",
        "tier": "high",
        "why": f"{target.name} has no safe restart configured; changing a deployment is a decision for a person",
        "recommendation": f"Roll {target.name} back to its previous deployment.",
        "rollback_plan": [
            f"Run: {rollback}",
            f"Watch the dashboard until {target.name} passes three probes in a row.",
            "If the previous deployment also fails, the cause is outside the code: check its dependencies and provider status.",
        ],
    }


class Executor:
    """Runs the mutating and read-only actions. Tests replace it with a spy."""

    def __init__(self, diagnostics_dir: Path):
        self.diagnostics_dir = diagnostics_dir

    def restart(self, spec: dict) -> str:
        if "http_post" in spec:
            req = urllib.request.Request(spec["http_post"], data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return f"POST {spec['http_post']} answered {resp.status}"
        if "command" in spec:
            out = subprocess.run(spec["command"], capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                raise RuntimeError(f"exit {out.returncode}: {out.stderr.strip()[:300]}")
            tail = out.stdout.strip()[:200]
            return f"command exited 0: {tail}" if tail else "command exited 0"
        raise ValueError(f"unknown restart spec {spec}")

    def collect_diagnostics(self, incident_id: int, target, evidence: list, event_detail: dict) -> str:
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        dns = None
        host = _host(target)
        if host:
            try:
                dns = sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
            except OSError as e:
                dns = f"lookup failed: {e}"
        path = self.diagnostics_dir / f"incident-{incident_id}.json"
        path.write_text(
            json.dumps({"target": target.name, "dns": dns, "event": event_detail, "probes": evidence}, indent=2),
            encoding="utf-8",
        )
        return str(path)


def _host(target) -> Optional[str]:
    if target.kind == "http":
        rest = target.endpoint.split("://", 1)[-1]
        return rest.split("/", 1)[0].rsplit(":", 1)[0] or None
    if target.kind == "tcp":
        return target.endpoint.rsplit(":", 1)[0]
    return None
