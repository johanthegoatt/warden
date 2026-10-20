"""detect -> verify -> diagnose -> decide -> act -> report

Each state is a method that takes the incident context and returns its output
and the next state. Every transition is written to the transitions table with
its input, output and duration, so an incident can be replayed step by step."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from . import actions, diagnose, llm, provider as provider_mod, report, store
from . import probe as probe_mod

FLOW = ("detect", "verify", "diagnose", "decide", "act", "report")


class Brain:
    def __init__(
        self,
        conn,
        reports_dir: Path,
        prober: Callable = probe_mod.check,
        model=None,
        executor: Optional[actions.Executor] = None,
        live: bool = False,
        confirm_attempts: int = 5,
        confirm_wait: float = 3.0,
        approval_ttl_hours: float = 24.0,
        provider=None,
        provider_every: int = 6,
    ):
        self.provider = provider
        self.provider_every = provider_every
        self.passes = 0
        self.conn = conn
        self.reports_dir = Path(reports_dir)
        self.prober = prober
        self.model = model
        self.executor = executor or actions.Executor(self.reports_dir / "diagnostics")
        self.live = live
        self.confirm_attempts = confirm_attempts
        self.confirm_wait = confirm_wait
        self.approval_ttl_hours = approval_ttl_hours

    # ---- entry points -------------------------------------------------

    def run_once(self) -> list:
        results = [self._resume(row) for row in self.conn.execute(
            "SELECT * FROM incidents WHERE state = 'awaiting_approval' ORDER BY id"
        ).fetchall()]
        results = [r for r in results if r]

        if self.provider is not None and self.passes % self.provider_every == 0:
            self._ask_provider()
        self.passes += 1

        # A recovery the sensor saw on its own needs no incident.
        self.conn.execute("UPDATE events SET claimed_by = 0 WHERE claimed_by IS NULL AND kind = 'recovered'")
        events = [dict(e) for e in self.conn.execute("SELECT * FROM events WHERE claimed_by IS NULL ORDER BY id")]
        # In a wide outage, fix what can be fixed first: a dependent's alert waits behind its cause.
        fixable = {name for name, t in store.targets(self.conn).items() if t.restart}
        events.sort(key=lambda e: (0 if e["kind"] == "failure" and e["fault"] == "target" and e["target"] in fixable
                                   else 1 if e["kind"] == "failure" and e["fault"] == "target" else 2, e["id"]))
        for event in events:
            results.append(self.handle(event))

        if not results:
            results.append({"verdict": "healthy", "detail": "no new events from the sensor"})
        store.beat(self.conn, "brain", {
            "mode": "live" if self.live else "dry-run",
            "model": getattr(self.model, "name", "none"),
            "last": results[-1].get("verdict"),
        })
        return results

    def _ask_provider(self) -> None:
        """Raises a failure for every target whose project the provider reports paused."""
        paused = self.provider.paused()
        if not paused:
            return
        busy = {r[0] for r in self.conn.execute(
            "SELECT target FROM incidents WHERE closed_at IS NULL "
            "UNION SELECT target FROM events WHERE claimed_by IS NULL AND kind = 'failure'")}
        for name, target in store.targets(self.conn).items():
            pid = provider_mod.project_of(target)
            if pid in paused and name not in busy:
                self.conn.execute(
                    "INSERT INTO events (ts, target, kind, fault, culprit, detail) VALUES (?, ?, 'failure', 'target', ?, ?)",
                    (store.now_iso(), name, name,
                     json.dumps({"source": self.provider.name, "provider_paused": True, "project": pid})),
                )

    def _still_paused(self, ctx: dict) -> Optional[bool]:
        paused = self.provider.paused() if self.provider is not None else None
        if paused is None:
            return None
        return ctx["event"]["detail"].get("project") in paused

    def handle(self, event: dict) -> dict:
        event["detail"] = json.loads(event.get("detail") or "{}")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            claimed = self.conn.execute(
                "SELECT claimed_by FROM events WHERE id = ?", (event["id"],)
            ).fetchone()
            if claimed is None or claimed["claimed_by"] is not None:
                self.conn.execute("ROLLBACK")
                return {"verdict": "skipped", "detail": f"event {event['id']} already handled"}
            cur = self.conn.execute(
                "INSERT INTO incidents (event_id, target, opened_at, state) VALUES (?, ?, ?, 'detect')",
                (event["id"], event["target"], store.now_iso()),
            )
            incident_id = int(cur.lastrowid)
            self.conn.execute("UPDATE events SET claimed_by = ? WHERE id = ?", (incident_id, event["id"]))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        ctx = self._context(incident_id, event)
        return self._drive(ctx, "detect")

    # ---- driver ------------------------------------------------------

    def _context(self, incident_id: int, event: dict) -> dict:
        all_targets = store.targets(self.conn)
        target = all_targets.get(event["target"])
        return {
            "incident_id": incident_id,
            "event": event,
            "target": target,
            "closure": store.upstream(all_targets, event["target"]) if target else [],
            "all_targets": all_targets,
            "cost": {"model": getattr(self.model, "name", "none"), "tokens_in": 0, "tokens_out": 0,
                     "wall_ms": 0.0, "cpu_ms": 0.0, "probes": 0, "model_calls": 0},
            "out": {},
            "mode": "live" if self.live else "dry-run",
            "since": store.evidence_floor(self.conn, event["target"], incident_id),
        }

    def _drive(self, ctx: dict, state: str) -> dict:
        while state not in ("closed", "awaiting_approval"):
            step = getattr(self, f"_{state}")
            wall, cpu = time.perf_counter(), time.process_time()
            given = self._input_for(ctx, state)
            output, nxt = step(ctx)
            wall_ms = (time.perf_counter() - wall) * 1000
            ctx["cost"]["wall_ms"] += wall_ms
            ctx["cost"]["cpu_ms"] += (time.process_time() - cpu) * 1000
            ctx["out"][state] = output
            self.conn.execute(
                "INSERT INTO transitions (incident_id, ts, state, input, output, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (ctx["incident_id"], store.now_iso(), state, json.dumps(given, default=str),
                 json.dumps(output, default=str), round(wall_ms, 2)),
            )
            self._save(ctx, nxt)
            state = nxt
        return self._summary(ctx)

    def _input_for(self, ctx: dict, state: str) -> dict:
        if state == "detect":
            e = ctx["event"]
            return {k: e.get(k) for k in ("id", "ts", "target", "kind", "fault", "culprit", "score")}
        prev = FLOW[FLOW.index(state) - 1] if state in FLOW and FLOW.index(state) > 0 else None
        return {"from": prev, "previous": ctx["out"].get(prev)} if prev else {}

    def _save(self, ctx: dict, state: str) -> None:
        out = ctx["out"]
        verdict = (out.get("verify") or {}).get("verdict")
        p = out.get("decide") or {}
        act = out.get("act") or {}
        closed = store.now_iso() if state == "closed" else None
        self.conn.execute(
            "UPDATE incidents SET state = ?, verdict = ?, tier = ?, action = ?, outcome = ?, cost = ?, "
            "closed_at = COALESCE(closed_at, ?), report_path = COALESCE(?, report_path), "
            "report_md = COALESCE(?, report_md) WHERE id = ?",
            (state, verdict, p.get("tier"), p.get("action"), act.get("outcome") or out.get("close_outcome"),
             json.dumps(_rounded(ctx["cost"])), closed, ctx.get("report_path"), ctx.get("report_md"),
             ctx["incident_id"]),
        )

    def _summary(self, ctx: dict) -> dict:
        out = ctx["out"]
        return {
            "incident_id": ctx["incident_id"],
            "target": ctx["event"]["target"],
            "verdict": (out.get("verify") or {}).get("verdict"),
            "action": (out.get("decide") or {}).get("action", "none"),
            "tier": (out.get("decide") or {}).get("tier"),
            "outcome": (out.get("act") or {}).get("outcome") or out.get("close_outcome"),
            "executed": (out.get("act") or {}).get("executed", False),
            "report": ctx.get("report_path"),
            "cost": _rounded(ctx["cost"]),
        }

    def _probe(self, ctx: dict, target) -> tuple:
        result = self.prober(target)
        ctx["cost"]["probes"] += 1
        pid = store.record_probe(self.conn, target.name, "brain", result)
        return pid, result

    # ---- states ------------------------------------------------------

    def _detect(self, ctx: dict):
        e = ctx["event"]
        if ctx["target"] is None:
            return {"summary": f"event for {e['target']}, which is no longer configured"}, "closed"
        what = {
            "failure": f"sensor raised a failure on {e['target']}, located at {e['fault']} ({e.get('culprit')})",
            "drift": f"sensor flagged latency drift on {e['target']} (z = {e.get('score')})",
        }.get(e["kind"], f"{e['kind']} event on {e['target']}")
        return {"summary": what, "kind": e["kind"], "sensor_fault": e["fault"], "culprit": e.get("culprit")}, "verify"

    def _verify(self, ctx: dict):
        e, target = ctx["event"], ctx["target"]
        pid, fresh = self._probe(ctx, target)
        deps = []
        for name in ctx["closure"]:
            dep = ctx["all_targets"].get(name)
            if dep:
                dpid, dres = self._probe(ctx, dep)
                deps.append({"name": name, "probe_id": dpid, "ok": dres.ok, "result": dres.summary()})
        fresh_out = {"probe_id": pid, "ok": fresh.ok, "result": fresh.summary(), "latency_ms": fresh.latency_ms}
        all_healthy = fresh.ok and all(d["ok"] for d in deps)
        base = {"brain_probe": fresh_out, "dependencies": deps, "all_fresh_healthy": all_healthy}

        project = provider_mod.project_of(target)
        if e["kind"] == "failure" and self.provider is not None and project:
            e["detail"].setdefault("project", project)
            paused = self._still_paused(ctx)
            base["provider"] = {"name": self.provider.name, "project": project, "paused": paused}
            if paused:
                v = {"verdict": "fault", "location": "target", "culprit": target.name, "provider_paused": True,
                     "summary": f"{self.provider.name} reports project {project} paused; "
                                f"the {fresh.summary()} the probe saw comes from the edge cache, not the app"}
                return {**v, **base}, "diagnose"
            if e["detail"].get("provider_paused"):
                v = {"verdict": "healthy", "location": "none", "culprit": None,
                     "summary": f"{self.provider.name} no longer reports {project} paused; it was unpaused before any action"}
                ctx["out"]["close_outcome"] = "unpaused before any action"
                return {**v, **base}, "closed"

        if e["kind"] == "failure" and e["fault"] == "probe":
            v = {"verdict": "probe_fault", "location": "probe", "culprit": e.get("culprit"),
                 "summary": f"not an outage: the sensor's {e.get('culprit')} failed while an independent path reached {target.name}"}
            return {**v, **base}, "diagnose"

        if e["kind"] == "failure":
            if fresh.ok:
                v = {"verdict": "healthy", "location": "none", "culprit": None,
                     "summary": f"{target.name} answers correctly now ({fresh.summary()}); it recovered before any action"}
                ctx["out"]["close_outcome"] = "recovered on its own before any action"
                return {**v, **base}, "closed"
            failing_dep = next((d for d in deps if not d["ok"]), None)
            if failing_dep:
                v = {"verdict": "fault", "location": "dependency", "culprit": failing_dep["name"],
                     "summary": f"confirmed: {target.name} fails and its dependency {failing_dep['name']} fails too"}
            else:
                v = {"verdict": "fault", "location": "target", "culprit": target.name,
                     "summary": f"confirmed by a third probe stack: {fresh.summary()}; dependencies pass"}
            return {**v, **base}, "diagnose"

        # drift: the service answers, the question is whether it is still slow
        drift = (e["detail"].get("drift") or {})
        budget = target.latency_budget_ms
        recent = [p["latency_ms"] for p in store.recent_probes(self.conn, [target.name], minutes=10)
                  if p["ok"] and p["path"] == "primary" and p["latency_ms"] is not None][-5:]
        recent_mean = sum(recent) / len(recent) if recent else (fresh.latency_ms or 0)
        usual = drift.get("base_mean_ms") or recent_mean
        still_slow = (not fresh.ok) or recent_mean > budget or recent_mean > usual * 1.25
        if not still_slow:
            v = {"verdict": "healthy", "location": "none", "culprit": None,
                 "summary": f"latency is back to normal: recent mean {recent_mean:.0f} ms against a usual {usual:.0f} ms"}
            ctx["out"]["close_outcome"] = "drift passed; nothing to do"
            return {**v, **base}, "closed"
        v = {"verdict": "degrading", "location": "target", "culprit": target.name,
             "reason": drift.get("reason") or f"recent mean {recent_mean:.0f} ms",
             "summary": f"still degraded: recent mean {recent_mean:.0f} ms, usual {usual:.0f} ms, budget {budget} ms"}
        return {**v, **base}, "diagnose"

    def _diagnose(self, ctx: dict):
        target, verdict = ctx["target"], ctx["out"]["verify"]
        evidence = store.recent_probes(self.conn, [target.name] + ctx["closure"], minutes=15, limit=40,
                                       since=ctx["since"])
        hypothesis = diagnose.rule_hypothesis(verdict, evidence, target, ctx["event"]["detail"])
        model_out = None
        if self.model is not None:
            prompt = diagnose.model_prompt(target, ctx["closure"], verdict, evidence)
            try:
                answer = self.model.ask(prompt)
                ctx["cost"]["model_calls"] += 1
                ctx["cost"]["tokens_in"] += answer.tokens_in
                ctx["cost"]["tokens_out"] += answer.tokens_out
                claim = llm.extract_json(answer.text)
                accepted, why = diagnose.ground(
                    claim, target.name, ctx["closure"], evidence, verdict["all_fresh_healthy"]
                )
                consistent = accepted and claim.get("verdict") == "fault" and claim.get("component") == hypothesis["component"]
                model_out = {"claim": claim, "accepted": accepted, "reason": why, "used": consistent}
                if consistent and claim.get("claim"):
                    hypothesis = {**hypothesis, "claim": str(claim["claim"])[:400], "worded_by": self.model.name}
            except llm.ModelUnavailable as exc:
                model_out = {"claim": None, "accepted": False, "reason": f"model unavailable: {exc}", "used": False}
        summary = hypothesis["claim"]
        return {"summary": summary, "hypothesis": hypothesis, "model": model_out,
                "evidence": [p["id"] for p in evidence]}, "decide"

    def _decide(self, ctx: dict):
        p = actions.plan(ctx["out"]["verify"], ctx["out"]["diagnose"]["hypothesis"], ctx["target"])
        tier = p.get("tier") or "none"
        p["summary"] = f"{p['action']} at tier {tier}: {p['why']}"
        if ctx.get("policy_note"):
            p["summary"] += f" ({ctx['policy_note']})"
        return p, "act"

    def _act(self, ctx: dict):
        p, target = ctx["out"]["decide"], ctx["target"]
        action, tier = p["action"], p.get("tier")
        out = {"action": action, "tier": tier, "executed": False, "dry_run": not self.live}

        if action == "collect_diagnostics":
            evidence = store.recent_probes(self.conn, [target.name] + ctx["closure"], minutes=15, since=ctx["since"])
            path = self.executor.collect_diagnostics(ctx["incident_id"], target, evidence, ctx["event"]["detail"])
            out.update(executed=True, result=f"diagnostics written to {_relative(path, self.reports_dir)}")
        elif tier == "high":
            out.update(result="recommendation only; high-tier actions are never executed",
                       recommendation=p.get("recommendation"), rollback_plan=p.get("rollback_plan"))
        elif action == "restart":
            if tier == "medium":
                decision = ctx.get("approval")
                if decision is None:
                    self.conn.execute(
                        "INSERT INTO approvals (incident_id, action, requested_at) VALUES (?, ?, ?)",
                        (ctx["incident_id"], f"restart {target.name}", store.now_iso()),
                    )
                    out.update(result="waiting for approval on the dashboard", outcome="awaiting approval",
                               summary=f"waiting for approval to restart {target.name}")
                    return out, "awaiting_approval"
                if decision != "approved":
                    out.update(result=f"approval {decision}; restart not run")
                    out.update(confirmation=self._confirm(ctx, target, attempts=1))
                    out["outcome"] = out["summary"] = _outcome(out)
                    return out, "report"
            if self.live:
                try:
                    out.update(executed=True, result=self.executor.restart(p["spec"]))
                except Exception as exc:  # report the failure, never retry blindly
                    out.update(executed=True, result=f"restart failed: {exc}", error=True)
            else:
                out.update(result=f"dry run: would restart {target.name} via {json.dumps(p['spec'])}")

        mutating = out["executed"] and action not in actions.READ_ONLY
        out["confirmation"] = self._confirm(ctx, target, attempts=self.confirm_attempts if mutating else 1)
        out["outcome"] = _outcome(out)
        out["summary"] = out["outcome"]
        return out, "report"

    def _confirm(self, ctx: dict, target, attempts: int) -> dict:
        """Rule 6: a fix counts only once a fresh probe passes after it."""
        tried = []
        for i in range(attempts):
            if i:
                time.sleep(self.confirm_wait)
            pid, res = self._probe(ctx, target)
            ok = res.ok
            result = res.summary()
            if ctx["out"].get("verify", {}).get("provider_paused"):
                # A cached 200 proves nothing here; the provider has to say the project runs again.
                paused = self._still_paused(ctx)
                ok = ok and paused is False
                result += {True: "; provider still reports it paused", False: "; provider reports it running",
                           None: "; provider could not be asked"}[paused]
            tried.append({"probe_id": pid, "ok": ok, "result": result})
            if ok:
                break
        return {"recovered": bool(tried and tried[-1]["ok"]), "probes": tried}

    def _report(self, ctx: dict):
        md, path = report.write(self.conn, ctx, self.reports_dir)
        ctx["report_md"], ctx["report_path"] = md, str(path)
        shown = _relative(path, self.reports_dir.parent)
        return {"summary": f"report written to {shown}", "path": shown}, "closed"

    # ---- approvals ---------------------------------------------------

    def _resume(self, incident) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE incident_id = ? ORDER BY id DESC LIMIT 1", (incident["id"],)
        ).fetchone()
        if row is None:
            return None
        event = dict(self.conn.execute("SELECT * FROM events WHERE id = ?", (incident["event_id"],)).fetchone())
        event["detail"] = json.loads(event.get("detail") or "{}")
        ctx = self._context(incident["id"], event)
        ctx["cost"].update(json.loads(incident["cost"] or "{}"))
        for t in self.conn.execute(
            "SELECT state, output FROM transitions WHERE incident_id = ? ORDER BY id", (incident["id"],)
        ):
            ctx["out"][t["state"]] = json.loads(t["output"])

        decision = row["decision"]
        if decision == "pending" and ctx["target"] is not None:
            # The config may have changed since the request; if the action is now low tier, nobody needs to approve it.
            now = actions.plan(ctx["out"]["verify"], ctx["out"]["diagnose"]["hypothesis"], ctx["target"])
            if now.get("tier") == "low":
                self.conn.execute("UPDATE approvals SET decision = 'approved', decided_at = ? WHERE id = ?",
                                  (store.now_iso(), row["id"]))
                ctx["policy_note"] = "warden.toml now marks this action low tier, so it runs without approval"
                return self._drive(ctx, "decide")
        if decision == "pending":
            expired = self.conn.execute(
                "SELECT ? < ?", (row["requested_at"], store.now_iso(-self.approval_ttl_hours * 3600))
            ).fetchone()[0]
            if not expired:
                return None
            self.conn.execute("UPDATE approvals SET decision = 'expired', decided_at = ? WHERE id = ?",
                              (store.now_iso(), row["id"]))
            decision = "expired"
        ctx["approval"] = decision
        return self._drive(ctx, "act")


def _outcome(out: dict) -> str:
    conf = out.get("confirmation") or {}
    recovered = conf.get("recovered")
    last = (conf.get("probes") or [{}])[-1]
    ref = f"probe #{last.get('probe_id')}" if last.get("probe_id") else "a fresh probe"
    action, executed = out["action"], out["executed"]
    if action == "restart" and executed and not out.get("error"):
        return (f"fixed: restart ran and {ref} passed" if recovered
                else f"not fixed: restart ran but {ref} still fails")
    if action == "restart" and out.get("error"):
        return f"restart failed; {'target recovered anyway per ' + ref if recovered else 'target still failing per ' + ref}"
    state = f"target answering per {ref}" if recovered else f"target still failing per {ref}"
    if action == "restart":
        return f"{out.get('result')}; {state}"
    if action == "rollback":
        return f"recommendation written, nothing executed; {state}"
    return f"{action.replace('_', ' ')} done; {state}"


def _relative(path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def _rounded(cost: dict) -> dict:
    return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in cost.items()}
