from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = Path(__file__).resolve().parents[2] / "schema.sql"


def now_iso(offset_seconds: float = 0.0) -> str:
    """Same text format the sensor writes through SQLite's strftime, so rows sort together."""
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


@dataclass
class Target:
    name: str
    kind: str
    endpoint: str
    expected_status: Optional[int]
    latency_budget_ms: int
    depends_on: list
    stateless: bool
    restart: Optional[dict]
    rollback: Optional[str]
    max_age_secs: Optional[int] = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Target":
        return cls(
            name=r["name"],
            kind=r["kind"],
            endpoint=r["endpoint"],
            expected_status=r["expected_status"],
            latency_budget_ms=r["latency_budget_ms"],
            depends_on=json.loads(r["depends_on"] or "[]"),
            stateless=bool(r["stateless"]),
            restart=json.loads(r["restart"]) if r["restart"] else None,
            rollback=r["rollback"],
            max_age_secs=r["max_age_secs"],
        )


def targets(conn: sqlite3.Connection) -> dict:
    return {r["name"]: Target.from_row(r) for r in conn.execute("SELECT * FROM targets")}


def upstream(all_targets: dict, name: str) -> list:
    """Transitive dependencies, nearest first. Mirrors graph::upstream in the sensor."""
    seen, order = set(), []
    queue = list(all_targets[name].depends_on) if name in all_targets else []
    while queue:
        n = queue.pop(0)
        if n == name or n in seen:
            continue
        seen.add(n)
        order.append(n)
        if n in all_targets:
            queue.extend(all_targets[n].depends_on)
    return order


def record_probe(conn: sqlite3.Connection, target: str, path: str, result: Any) -> int:
    cur = conn.execute(
        "INSERT INTO probes (ts, target, path, ok, status, latency_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), target, path, int(result.ok), result.status, result.latency_ms, result.error),
    )
    return int(cur.lastrowid)


def recent_probes(conn: sqlite3.Connection, names: list, minutes: float = 15.0, limit: int = 40,
                  since: Optional[str] = None) -> list:
    if not names:
        return []
    marks = ",".join("?" for _ in names)
    floor = max(now_iso(-minutes * 60), since or "")
    rows = conn.execute(
        f"SELECT * FROM probes WHERE target IN ({marks}) AND ts > ? ORDER BY id DESC LIMIT ?",
        (*names, floor, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def evidence_floor(conn: sqlite3.Connection, target: str, incident_id: int) -> Optional[str]:
    """Evidence for an incident starts after the previous incident on the same target closed."""
    row = conn.execute(
        "SELECT MAX(closed_at) FROM incidents WHERE target = ? AND id < ? AND closed_at IS NOT NULL",
        (target, incident_id),
    ).fetchone()
    return row[0] if row else None


def beat(conn: sqlite3.Connection, component: str, note: dict) -> None:
    conn.execute(
        "INSERT INTO heartbeat (component, ts, note) VALUES (?, ?, ?) "
        "ON CONFLICT(component) DO UPDATE SET ts = excluded.ts, note = excluded.note",
        (component, now_iso(), json.dumps(note)),
    )
