"""One test class per numbered rule in the build brief."""

from __future__ import annotations

import json
import unittest

from helpers import DOWN, ERR500, UP, FakeProber, ScriptedModel, SpyExecutor, World

from warden_brain import diagnose
from warden_brain.machine import FLOW


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()


class Rule1HealthyIsAValidAnswer(Base):
    def test_no_events_means_healthy_and_nothing_done(self):
        self.w.target("site")
        self.w.probe("site", "primary", True, 200)
        spy = SpyExecutor()
        results = self.w.brain(FakeProber({"site": UP}), executor=spy, live=True).run_once()
        self.assertEqual(results, [{"verdict": "healthy", "detail": "no new events from the sensor"}])
        self.assertEqual(self.w.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
        self.assertEqual(spy.restarts, [])

    def test_failure_that_rechecks_healthy_is_closed_without_action_or_model(self):
        self.w.target("site", stateless=True, restart={"http_post": "http://x/restart"})
        self.w.failure("site", "target")
        model = ScriptedModel({"verdict": "fault", "component": "site", "claim": "site is down", "evidence": [1]})
        spy = SpyExecutor()
        [r] = self.w.brain(FakeProber({"site": UP}), model=model, executor=spy, live=True).run_once()
        self.assertEqual(r["verdict"], "healthy")
        self.assertEqual(r["action"], "none")
        self.assertEqual(spy.restarts, [])
        self.assertEqual(model.prompts, [], "a healthy system must not be handed to the model")
        inc = self.w.incident(r["incident_id"])
        self.assertEqual(inc["state"], "closed")
        self.assertIsNone(inc["report_md"])

    def test_model_inventing_an_incident_on_a_healthy_system_is_rejected(self):
        evidence = [{"id": 7, "ok": 1, "target": "site"}]
        claim = {"verdict": "fault", "component": "site", "claim": "memory leak", "evidence": [7]}
        accepted, why = diagnose.ground(claim, "site", [], evidence, fresh_healthy=True)
        self.assertFalse(accepted)
        self.assertIn("every fresh probe", why)

    def test_drift_that_has_passed_is_healthy(self):
        self.w.target("site")
        self.w.drift("site", base=100.0, recent=105.0)
        [r] = self.w.brain(FakeProber({"site": UP})).run_once()
        self.assertEqual(r["verdict"], "healthy")


class Rule2AFailedProbeIsNotAnOutage(Base):
    def test_probe_path_fault_never_touches_the_target(self):
        self.w.target("site", stateless=True, restart={"http_post": "http://x/restart"})
        self.w.failure("site", "probe", culprit="primary probe path", secondary_ok=True)
        spy = SpyExecutor()
        [r] = self.w.brain(FakeProber({"site": UP}), executor=spy, live=True).run_once()
        self.assertEqual(r["verdict"], "probe_fault")
        self.assertEqual(r["action"], "collect_diagnostics")
        self.assertEqual(spy.restarts, [])
        md = self.w.incident(r["incident_id"])["report_md"]
        self.assertIn("Nothing is wrong with site", md)

    def test_failing_dependency_is_blamed_instead_of_the_target(self):
        self.w.target("discord")
        self.w.target("bot", depends_on=["discord"], stateless=True, restart={"http_post": "http://x/restart"})
        self.w.failure("bot", "target")
        spy = SpyExecutor()
        [r] = self.w.brain(FakeProber({"bot": DOWN, "discord": DOWN}), executor=spy, live=True).run_once()
        verify = json.loads(self.w.transitions(r["incident_id"])[1]["output"])
        self.assertEqual(verify["location"], "dependency")
        self.assertEqual(verify["culprit"], "discord")
        self.assertEqual(spy.restarts, [], "restarting the bot cannot fix discord")

    def test_model_naming_a_component_outside_the_graph_is_rejected(self):
        evidence = [{"id": 3, "ok": 0, "target": "bot"}]
        claim = {"verdict": "fault", "component": "postgres", "claim": "db is down", "evidence": [3]}
        accepted, why = diagnose.ground(claim, "bot", ["discord"], evidence, fresh_healthy=False)
        self.assertFalse(accepted)
        self.assertIn("not bot or one of its dependencies", why)

    def test_model_citing_probes_that_do_not_exist_is_rejected(self):
        evidence = [{"id": 3, "ok": 0, "target": "bot"}]
        claim = {"verdict": "fault", "component": "bot", "claim": "bot crashed", "evidence": [3, 99]}
        accepted, why = diagnose.ground(claim, "bot", [], evidence, fresh_healthy=False)
        self.assertFalse(accepted)
        self.assertIn("[99]", why)


class Rule3PipelineIsAStateMachine(Base):
    def test_every_state_logs_input_and_output_in_order(self):
        self.w.target("site", rollback="vercel rollback site")
        self.w.failure("site", "target")
        [r] = self.w.brain(FakeProber({"site": ERR500})).run_once()
        rows = self.w.transitions(r["incident_id"])
        self.assertEqual([t["state"] for t in rows], list(FLOW))
        for t in rows:
            self.assertIsInstance(json.loads(t["input"]), dict)
            self.assertTrue(json.loads(t["output"]).get("summary"), f"{t['state']} has no summary")
        self.assertEqual(json.loads(rows[1]["input"])["from"], "detect")

    def test_evidence_stops_at_the_previous_incident(self):
        self.w.target("site", rollback="vercel rollback site")
        self.w.failure("site", "target")
        b = self.w.brain(FakeProber({"site": ERR500}))
        [first] = b.run_once()
        first_probe_ids = {r[0] for r in self.w.conn.execute("SELECT id FROM probes")}
        self.w.failure("site", "probe", culprit="primary probe path", secondary_ok=True)
        b.prober = FakeProber({"site": UP})
        [second] = b.run_once()
        diag = json.loads(self.w.transitions(second["incident_id"])[2]["output"])
        self.assertFalse(set(diag["evidence"]) & first_probe_ids, "incident 2 cited incident 1's probes")
        self.assertTrue(diag["hypothesis"]["evidence"])

    def test_an_event_is_handled_once(self):
        self.w.target("site")
        self.w.failure("site", "target")
        b = self.w.brain(FakeProber({"site": ERR500}))
        self.assertEqual(len([r for r in b.run_once() if r.get("incident_id")]), 1)
        self.assertEqual(b.run_once(), [{"verdict": "healthy", "detail": "no new events from the sensor"}])


class Rule4ThreeActionTiers(Base):
    def test_low_tier_restart_runs_in_live_mode(self):
        self.w.target("api", stateless=True, restart={"http_post": "http://x/restart"})
        self.w.failure("api", "target")
        spy = SpyExecutor()
        [r] = self.w.brain(FakeProber({"api": [ERR500, UP]}), executor=spy, live=True).run_once()
        self.assertEqual((r["tier"], r["action"], r["executed"]), ("low", "restart", True))
        self.assertEqual(spy.restarts, [{"http_post": "http://x/restart"}])

    def test_dry_run_is_the_default_and_executes_nothing(self):
        self.w.target("api", stateless=True, restart={"http_post": "http://x/restart"})
        self.w.failure("api", "target")
        spy = SpyExecutor()
        [r] = self.w.brain(FakeProber({"api": ERR500}), executor=spy).run_once()
        self.assertEqual(spy.restarts, [])
        self.assertIn("dry run: would restart api", r["outcome"])

    def test_medium_tier_waits_for_approval_then_runs(self):
        self.w.target("worker", stateless=False, restart={"command": ["true"]})
        self.w.failure("worker", "target")
        spy = SpyExecutor()
        b = self.w.brain(FakeProber({"worker": [ERR500, ERR500, UP]}), executor=spy, live=True)
        [r] = b.run_once()
        self.assertEqual(r["outcome"], "awaiting approval")
        self.assertEqual(spy.restarts, [])
        self.assertEqual(self.w.incident(r["incident_id"])["state"], "awaiting_approval")
        self.assertEqual(b.run_once()[0]["verdict"], "healthy", "still pending, nothing new")
        self.w.conn.execute("UPDATE approvals SET decision = 'approved' WHERE incident_id = ?", (r["incident_id"],))
        [r2] = b.run_once()
        self.assertEqual(spy.restarts, [{"command": ["true"]}])
        self.assertTrue(r2["outcome"].startswith("fixed"), r2["outcome"])

    def test_waiting_action_runs_once_config_makes_it_low_tier(self):
        self.w.target("site", stateless=False, restart={"command": ["unpause"]})
        self.w.failure("site", "target")
        spy = SpyExecutor()
        b = self.w.brain(FakeProber({"site": [ERR500, ERR500, UP]}), executor=spy, live=True)
        [r] = b.run_once()
        self.assertEqual(r["outcome"], "awaiting approval")
        self.w.conn.execute("UPDATE targets SET stateless = 1 WHERE name = 'site'")
        [r2] = b.run_once()
        self.assertEqual(spy.restarts, [{"command": ["unpause"]}])
        self.assertEqual(r2["tier"], "low")
        self.assertTrue(r2["outcome"].startswith("fixed"), r2["outcome"])

    def test_denied_medium_action_is_not_run(self):
        self.w.target("worker", restart={"command": ["true"]})
        self.w.failure("worker", "target")
        spy = SpyExecutor()
        b = self.w.brain(FakeProber({"worker": ERR500}), executor=spy, live=True)
        [r] = b.run_once()
        self.w.conn.execute("UPDATE approvals SET decision = 'denied' WHERE incident_id = ?", (r["incident_id"],))
        [r2] = b.run_once()
        self.assertEqual(spy.restarts, [])
        self.assertIn("approval denied", r2["outcome"])

    def test_high_tier_is_never_executed_even_live(self):
        self.w.target("site", rollback="vercel rollback site")
        self.w.failure("site", "target")
        spy = SpyExecutor()
        [r] = self.w.brain(FakeProber({"site": ERR500}), executor=spy, live=True).run_once()
        self.assertEqual((r["tier"], r["action"], r["executed"]), ("high", "rollback", False))
        self.assertEqual(spy.restarts, [])
        md = self.w.incident(r["incident_id"])["report_md"]
        self.assertIn("1. Run: vercel rollback site", md)


class Rule5EveryIncidentRecordsItsCost(Base):
    def test_tokens_probes_and_time_are_recorded(self):
        self.w.target("site", rollback="vercel rollback site")
        self.w.failure("site", "target")
        model = ScriptedModel({"verdict": "fault", "component": "site",
                               "claim": "site returns 500 from its application", "evidence": [1]}, tokens=(321, 45))
        [r] = self.w.brain(FakeProber({"site": ERR500}), model=model).run_once()
        cost = json.loads(self.w.incident(r["incident_id"])["cost"])
        self.assertEqual((cost["tokens_in"], cost["tokens_out"], cost["model_calls"]), (321, 45, 1))
        self.assertEqual(cost["model"], "scripted:test")
        self.assertGreaterEqual(cost["probes"], 2)
        self.assertGreater(cost["wall_ms"], 0)
        md = self.w.incident(r["incident_id"])["report_md"]
        self.assertIn("| scripted:test | 1 | 321 | 45 |", md)

    def test_grounded_model_claim_is_used_and_marked(self):
        self.w.target("site", rollback="vercel rollback site")
        self.w.failure("site", "target")
        model = ScriptedModel({"verdict": "fault", "component": "site",
                               "claim": "The site's application layer is throwing 500s.", "evidence": [1]})
        [r] = self.w.brain(FakeProber({"site": ERR500}), model=model).run_once()
        diag = json.loads(self.w.transitions(r["incident_id"])[2]["output"])
        self.assertTrue(diag["model"]["accepted"])
        self.assertEqual(diag["hypothesis"]["worded_by"], "scripted:test")


class Rule6NoFixWithoutAFreshProbe(Base):
    def test_restart_with_target_still_failing_is_not_fixed(self):
        self.w.target("api", stateless=True, restart={"http_post": "http://x/restart"})
        self.w.failure("api", "target")
        [r] = self.w.brain(FakeProber({"api": ERR500}), live=True).run_once()
        self.assertTrue(r["outcome"].startswith("not fixed"), r["outcome"])
        md = self.w.incident(r["incident_id"])["report_md"]
        self.assertNotIn("Outcome: fixed", md)
        self.assertEqual(md.count("- Probe #"), 3, "three confirmation attempts, all reported")

    def test_fixed_names_the_probe_that_confirmed_it(self):
        self.w.target("api", stateless=True, restart={"http_post": "http://x/restart"})
        self.w.failure("api", "target")
        [r] = self.w.brain(FakeProber({"api": [ERR500, ERR500, UP]}), live=True).run_once()
        self.assertTrue(r["outcome"].startswith("fixed"), r["outcome"])
        last = self.w.conn.execute(
            "SELECT id, ok, path FROM probes WHERE target = 'api' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((last["ok"], last["path"]), (1, "brain"))
        self.assertIn(f"probe #{last['id']} passed", r["outcome"])

    def test_failed_restart_is_reported_as_failed(self):
        self.w.target("api", stateless=True, restart={"http_post": "http://x/restart"})
        self.w.failure("api", "target")
        spy = SpyExecutor(restart_result=RuntimeError("connection refused"))
        [r] = self.w.brain(FakeProber({"api": ERR500}), executor=spy, live=True).run_once()
        self.assertTrue(r["outcome"].startswith("restart failed"), r["outcome"])


if __name__ == "__main__":
    unittest.main()
