"""A paused project can keep answering 200 from the edge cache. The provider's API decides."""

from __future__ import annotations

import unittest

from helpers import ERR500, UP, FakeProber, SpyExecutor, World

UNPAUSE = {"command": ["vercel.cmd", "api", "/v1/projects/prj_abc/unpause?slug=team", "-X", "POST"]}


class FakeProvider:
    name = "vercel"

    def __init__(self, paused):
        self.state = paused

    def paused(self):
        return None if self.state is None else set(self.state)


class UnpausingExecutor(SpyExecutor):
    def __init__(self, provider, takes=True):
        super().__init__()
        self.provider, self.takes = provider, takes

    def restart(self, spec):
        super().restart(spec)
        if self.takes:
            self.provider.state.discard("prj_abc")
        return "unpaused"


class ProviderIsTheSecondSourceOfTruth(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.target("site", stateless=True, restart=UNPAUSE)

    def tearDown(self):
        self.w.close()

    def brain(self, provider, executor):
        b = self.w.brain(FakeProber({"site": UP}), executor=executor, live=True)
        b.provider = provider
        return b

    def test_paused_project_behind_a_cached_200_is_unpaused_and_confirmed(self):
        provider = FakeProvider({"prj_abc"})
        spy = UnpausingExecutor(provider)
        [r] = self.brain(provider, spy).run_once()
        self.assertEqual((r["verdict"], r["action"], r["tier"]), ("fault", "restart", "low"))
        self.assertEqual(spy.restarts, [UNPAUSE])
        self.assertTrue(r["outcome"].startswith("fixed"), r["outcome"])
        md = self.w.incident(r["incident_id"])["report_md"]
        self.assertIn("reports project prj_abc as paused", md)
        self.assertIn("provider reports it running", md)

    def test_a_200_is_not_a_fix_while_the_provider_still_says_paused(self):
        provider = FakeProvider({"prj_abc"})
        [r] = self.brain(provider, UnpausingExecutor(provider, takes=False)).run_once()
        self.assertTrue(r["outcome"].startswith("not fixed"), r["outcome"])

    def test_an_unreachable_provider_raises_nothing(self):
        [r] = self.brain(FakeProvider(None), SpyExecutor()).run_once()
        self.assertEqual(r["verdict"], "healthy")

    def test_fixable_failures_are_handled_before_their_dependents(self):
        self.w.target("sub", depends_on=["site"])
        self.w.failure("sub", "dependency", culprit="site")
        self.w.failure("site", "target")
        b = self.w.brain(FakeProber({"site": [ERR500, UP], "sub": UP}), executor=SpyExecutor(), live=True)
        results = b.run_once()
        self.assertEqual([r["target"] for r in results], ["site", "sub"])


if __name__ == "__main__":
    unittest.main()
