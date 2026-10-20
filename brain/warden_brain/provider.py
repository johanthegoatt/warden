"""Asks the hosting provider what state each project is in.

A paused Vercel project keeps answering 200 from Vercel's edge cache for
pages it has cached, so HTTP probes alone can miss it. The provider's own
API is the second source of truth. Targets map to projects through the
project id in their restart command."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable, Optional

# The brain runs without a console; without this every vercel.cmd call flashes a window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROJECT_ID = re.compile(r"/v1/projects/(prj_[A-Za-z0-9]+)/")


def project_of(target) -> Optional[str]:
    spec = target.restart or {}
    text = " ".join(spec.get("command", [])) if "command" in spec else spec.get("http_post", "")
    m = PROJECT_ID.search(text or "")
    return m.group(1) if m else None


class Vercel:
    name = "vercel"

    def __init__(self, scope: str, runner: Optional[Callable] = None):
        self.scope = scope
        self.runner = runner or subprocess.run

    def paused(self) -> Optional[set]:
        """Ids of paused projects, or None when the API cannot be reached (never guess)."""
        try:
            out = self.runner(
                ["vercel.cmd", "api", f"/v9/projects?limit=100&slug={self.scope}", "--raw"],
                capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW,
            )
            data = json.loads(out.stdout[out.stdout.find("{"):])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
        return {p["id"] for p in data.get("projects", []) if p.get("paused")}
