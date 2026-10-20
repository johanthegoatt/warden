from __future__ import annotations

import json
import tempfile
from pathlib import Path

from warden_brain import store
from warden_brain.llm import Answer
from warden_brain.machine import Brain
from warden_brain.probe import Result


class FakeProber:
    """Answers per target name; a list is consumed one answer per probe."""

    def __init__(self, answers: dict):
        self.answers = {k: (v if isinstance(v, list) else [v]) for k, v in answers.items()}
        self.calls = []

    def __call__(self, target):
        self.calls.append(target.name)
        seq = self.answers[target.name]
        return seq.pop(0) if len(seq) > 1 else seq[0]


class SpyExecutor:
    def __init__(self, restart_result="restarted"):
        self.restarts = []
        self.diagnostics = []
        self.restart_result = restart_result

    def restart(self, spec):
        self.restarts.append(spec)
        if isinstance(self.restart_result, Exception):
            raise self.restart_result
        return self.restart_result

    def collect_diagnostics(self, incident_id, target, evidence, detail):
        self.diagnostics.append(incident_id)
        return f"diagnostics/incident-{incident_id}.json"


class ScriptedModel:
    name = "scripted:test"

    def __init__(self, reply: dict, tokens=(120, 30)):
        self.reply = reply
        self.tokens = tokens
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return Answer(json.dumps(self.reply), *self.tokens)


UP = Result(True, 200, 90.0)
DOWN = Result(False, None, 8000.0, "TimeoutError: timed out")
ERR500 = Result(False, 500, 120.0, "status 500, expected 200")


class World:
    """A temporary database with targets, probes and events written the way the sensor writes them."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = store.connect(self.dir / "warden.db")

    def close(self):
        self.conn.close()
        self.tmp.cleanup()

    def target(self, name, depends_on=(), stateless=False, restart=None, rollback=None, kind="http"):
        self.conn.execute(
            "INSERT INTO targets (name, kind, endpoint, expected_status, latency_budget_ms, depends_on, "
            "stateless, restart, rollback, updated_at) VALUES (?, ?, ?, 200, 1000, ?, ?, ?, ?, ?)",
            (name, kind, f"http://127.0.0.1:1/{name}", json.dumps(list(depends_on)), int(stateless),
             json.dumps(restart) if restart else None, rollback, store.now_iso()),
        )

    def probe(self, target, path, ok, status=None, latency=100.0, error=None):
        return store.record_probe(self.conn, target, path, Result(ok, status, latency, error))

    def failure(self, target, fault, culprit=None, primary_error="timed out", secondary_ok=False):
        p = self.probe(target, "primary", False, None, 8000.0, primary_error)
        s = self.probe(target, "secondary", secondary_ok, 200 if secondary_ok else None, 300.0,
                       None if secondary_ok else "connect refused")
        c = self.probe(target, "canary", True, None, 20.0)
        detail = {
            "probe_ids": {"primary": p, "secondary": s, "canary": c},
            "primary": {"ok": False, "error": primary_error},
            "secondary": {"ok": secondary_ok, "status": 200 if secondary_ok else None,
                          "error": None if secondary_ok else "connect refused"},
            "canary": {"ok": True},
        }
        return self._event(target, "failure", fault, culprit or target, None, detail)

    def drift(self, target, base=100.0, recent=400.0):
        for _ in range(5):
            self.probe(target, "primary", True, 200, recent)
        detail = {"drift": {"base_mean_ms": base, "recent_mean_ms": recent, "z": 6.0,
                            "reason": f"recent mean {recent:.0f} ms is 6.0 standard deviations above the usual {base:.0f} ms"}}
        return self._event(target, "drift", "target", target, 6.0, detail)

    def _event(self, target, kind, fault, culprit, score, detail):
        cur = self.conn.execute(
            "INSERT INTO events (ts, target, kind, fault, culprit, score, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (store.now_iso(), target, kind, fault, culprit, score, json.dumps(detail)),
        )
        return int(cur.lastrowid)

    def brain(self, prober, model=None, executor=None, live=False):
        return Brain(self.conn, self.dir / "reports", prober=prober, model=model,
                     executor=executor or SpyExecutor(), live=live, confirm_attempts=3, confirm_wait=0)

    def transitions(self, incident_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT state, input, output FROM transitions WHERE incident_id = ? ORDER BY id", (incident_id,))]

    def incident(self, incident_id):
        return dict(self.conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone())
