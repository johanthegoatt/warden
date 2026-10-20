"""Runs the whole system against two fake targets and breaks them on purpose.

Three incidents, each a different branch of the state machine:
  1. fake-api returns 500        -> target fault, low-tier restart, confirmed by a fresh probe
  2. fake-api rejects one client -> probe fault, target left alone
  3. fake-worker stops answering -> medium-tier restart, approved through the dashboard API

The reports land in reports/. Usage: python scripts/demo.py [--keep-running]
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
EXE = ROOT / "sensor" / "target" / "debug" / ("warden-sensor.exe" if os.name == "nt" else "warden-sensor")
DASH = "127.0.0.1:7879"

CONFIG = """
[sensor]
interval_secs = 5
db = "warden.db"
dashboard = "{dash}"
canary = "127.0.0.1:{canary}"
timeout_ms = 2000
baseline_window = 30

[[target]]
name = "fake-api"
kind = "http"
url = "http://127.0.0.1:8099/"
expected_status = 200
latency_budget_ms = 800
stateless = true
restart_url = "http://127.0.0.1:8098/restart"

[[target]]
name = "fake-worker"
kind = "http"
url = "http://127.0.0.1:8097/"
expected_status = 200
latency_budget_ms = 800
depends_on = ["fake-api"]
restart_url = "http://127.0.0.1:8096/restart"
"""


def post(url: str, body: bytes = b"", headers=None) -> str:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


def wait_for(conn, sql: str, params=(), timeout: float = 90.0, what: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute(sql, params).fetchone()
        if row:
            return row
        time.sleep(1)
    raise TimeoutError(f"gave up waiting for {what}")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    if not EXE.exists():
        print(f"build the sensor first: cargo build --manifest-path sensor/Cargo.toml  ({EXE} missing)")
        return 2
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir()
    (DEMO / "warden.toml").write_text(CONFIG.format(dash=DASH, canary=8098), encoding="utf-8")

    procs = []

    def spawn(cmd, cwd=ROOT, env=None):
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    try:
        spawn([str(EXE), "fake", "--port", "8099", "--control", "8098"])
        spawn([str(EXE), "fake", "--port", "8097", "--control", "8096"])
        time.sleep(0.5)
        spawn([str(EXE), "run", "--config", str(DEMO / "warden.toml")])
        env = {**os.environ, "PYTHONPATH": str(ROOT / "brain")}
        db = DEMO / "warden.db"
        wait_for_file(db)
        spawn([sys.executable, "-m", "warden_brain", "--db", str(db), "--reports", str(ROOT / "reports"),
               "--live", "--interval", "2"], env=env)
        conn = sqlite3.connect(str(db), timeout=5)
        conn.row_factory = sqlite3.Row
        print(f"dashboard: http://{DASH}  (sensor every 5 s, brain every 2 s, live mode)")

        print("baseline: 20 s of healthy probes")
        time.sleep(20)

        print("1. fake-api starts returning 500")
        post("http://127.0.0.1:8098/mode/error")
        row = wait_for(conn, "SELECT * FROM incidents WHERE target='fake-api' AND closed_at IS NOT NULL ORDER BY id DESC",
                       what="incident 1 to close")
        print(f"   incident {row['id']}: {row['verdict']}, {row['action']} ({row['tier']}), {row['outcome']}")
        time.sleep(12)

        print("2. fake-api rejects only the primary probe's client")
        post("http://127.0.0.1:8098/mode/primary-only")
        row = wait_for(conn, "SELECT * FROM incidents WHERE target='fake-api' AND verdict='probe_fault' AND closed_at IS NOT NULL",
                       what="incident 2 to close")
        print(f"   incident {row['id']}: {row['verdict']}, {row['action']} ({row['tier']}), {row['outcome']}")
        post("http://127.0.0.1:8098/mode/healthy")
        time.sleep(12)

        print("3. fake-worker stops answering; it is not stateless, so the restart needs approval")
        post("http://127.0.0.1:8096/mode/down")
        appr = wait_for(conn, "SELECT * FROM approvals WHERE decision='pending'", what="an approval request")
        print(f"   approval {appr['id']} pending: {appr['action']}; approving through the dashboard API")
        time.sleep(4)
        post(f"http://{DASH}/api/approvals/{appr['id']}", b"approve", {"X-Warden-Request": "1"})
        row = wait_for(conn, "SELECT * FROM incidents WHERE id = ? AND closed_at IS NOT NULL", (appr["incident_id"],),
                       what="incident 3 to close")
        print(f"   incident {row['id']}: {row['verdict']}, {row['action']} ({row['tier']}), {row['outcome']}")

        reports = sorted((ROOT / "reports").glob("incident-*.md"))
        print("reports:\n  " + "\n  ".join(str(p.relative_to(ROOT)) for p in reports))
        if "--keep-running" in sys.argv:
            print("still running; Ctrl+C to stop")
            while True:
                time.sleep(60)
        return 0
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while not path.exists():
        if time.time() > deadline:
            raise TimeoutError(f"{path} never appeared; did the sensor start?")
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())
