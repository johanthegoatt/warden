"""Warden's cloud sentinel: the rescue path that keeps working while the PC is off.

Runs from GitHub Actions every five minutes. It reads the same warden.toml,
probes every http target, asks Vercel which projects are paused, unpauses
the ones a target maps to, and confirms through Vercel and a fresh probe.
It exits 1 when something is still down, so GitHub emails the repo owner.

Needs VERCEL_TOKEN in the environment to act; without it the run only reports.
Standard library only (Python 3.11+ for tomllib).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

PROJECT = re.compile(r"/v1/projects/(prj_[A-Za-z0-9]+)/unpause\?slug=([A-Za-z0-9_-]+)")
API = "https://api.vercel.com"


@dataclass
class Target:
    name: str
    url: str
    expected: int
    project: Optional[str]
    team: Optional[str]


def load(path: Path) -> list:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    for t in cfg.get("target", []):
        if t.get("kind") != "http" or not t.get("enabled", True):
            continue
        m = PROJECT.search(" ".join(t.get("restart_cmd", [])))
        out.append(Target(t["name"], t["url"], t.get("expected_status", 200),
                          m.group(1) if m else None, m.group(2) if m else None))
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def probe(t: Target) -> tuple:
    req = urllib.request.Request(t.url, headers={"User-Agent": "warden-sentinel/0.1"})
    try:
        with _opener.open(req, timeout=15) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:  # network errors arrive as many unrelated types
        return False, f"{type(e).__name__}: {e}"
    return status == t.expected, f"status {status}"


class Vercel:
    def __init__(self, token: str, send: Optional[Callable] = None):
        self.token = token
        self.send = send or self._send

    def _send(self, method: str, path: str) -> dict:
        req = urllib.request.Request(API + path, method=method, data=b"" if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
        return json.loads(body) if body else {}

    def paused(self, team: str) -> set:
        data = self.send("GET", f"/v9/projects?limit=100&slug={team}")
        return {p["id"] for p in data.get("projects", []) if p.get("paused")}

    def unpause(self, project: str, team: str) -> None:
        self.send("POST", f"/v1/projects/{project}/unpause?slug={team}")


def run(targets: list, vercel: Optional[Vercel], prober: Callable = probe, wait: float = 10.0,
        attempts: int = 3) -> tuple:
    """Returns (report lines, names still down)."""
    lines = []
    teams = sorted({t.team for t in targets if t.team})
    paused = set()
    if vercel:
        for team in teams:
            try:
                paused |= vercel.paused(team)
            except Exception as e:
                lines.append(f"- Could not ask Vercel about team {team}: {e}")

    with ThreadPoolExecutor(16) as pool:
        first = dict(zip([t.name for t in targets], pool.map(prober, targets)))

    # A paused project can still answer 200 from the edge cache, so Vercel's flag counts as down.
    broken = [t for t in targets if not first[t.name][0] or (t.project and t.project in paused)]
    fixed_projects = set()
    for t in broken:
        if vercel and t.project and t.project in paused and t.project not in fixed_projects:
            try:
                vercel.unpause(t.project, t.team)
                fixed_projects.add(t.project)
                lines.append(f"- Unpaused {t.project} for {t.name}.")
            except Exception as e:
                lines.append(f"- Unpausing {t.project} for {t.name} failed: {e}")

    still_down = []
    for t in broken:
        ok, result = False, first[t.name][1]
        for i in range(attempts):
            if i or fixed_projects:
                time.sleep(wait)
            ok, result = prober(t)
            if ok and vercel and t.project:
                try:
                    ok = t.project not in vercel.paused(t.team)
                    result += "" if ok else ", Vercel still reports it paused"
                except Exception as e:
                    ok, result = False, f"{result}, Vercel could not confirm: {e}"
            if ok:
                break
        state = "fixed" if ok and t.project in fixed_projects else ("recovered" if ok else "STILL DOWN")
        lines.append(f"- {t.name}: {state} ({result}).")
        if not ok:
            still_down.append(t.name)
    healthy = len(targets) - len(broken)
    lines.insert(0, f"{healthy} of {len(targets)} targets healthy on the first probe; "
                    f"{len(broken)} needed attention; {len(still_down)} still down.")
    return lines, still_down


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    targets = load(root / "warden.toml")
    token = os.environ.get("VERCEL_TOKEN", "").strip()
    vercel = Vercel(token) if token else None
    lines, down = run(targets, vercel)
    if not vercel:
        lines.insert(0, "VERCEL_TOKEN is not set, so this run could only report, not unpause.")
    text = "## Warden sentinel\n\n" + "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    return 1 if down else 0


if __name__ == "__main__":
    sys.exit(main())
