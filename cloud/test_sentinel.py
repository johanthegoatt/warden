from __future__ import annotations

import unittest
from pathlib import Path

import sentinel
from sentinel import Target

T = lambda name, project=None: Target(name, f"https://{name}/", 200, project, "team" if project else None)


class FakeVercel:
    def __init__(self, paused, takes=True):
        self.state, self.takes, self.unpaused = set(paused), takes, []

    def paused(self, team):
        return set(self.state)

    def unpause(self, project, team):
        self.unpaused.append(project)
        if self.takes:
            self.state.discard(project)


class Sentinel(unittest.TestCase):
    def test_config_maps_targets_to_vercel_projects(self):
        targets = sentinel.load(Path(__file__).resolve().parents[1] / "warden.toml")
        site = next(t for t in targets if t.name == "johanthegoat-site")
        self.assertEqual((site.project, site.team), ("prj_GmYPTqFqVYunRZBa0CZvAQGTrWig", "neonices-projects"))
        self.assertFalse(any(t.name == "nexusonline-sender" for t in targets), "heartbeats are local-only")

    def test_all_healthy_does_nothing(self):
        v = FakeVercel([])
        lines, down = sentinel.run([T("a", "prj_a"), T("b")], v, prober=lambda t: (True, "status 200"), wait=0)
        self.assertEqual(down, [])
        self.assertEqual(v.unpaused, [])
        self.assertTrue(lines[0].startswith("2 of 2 targets healthy"))

    def test_paused_project_behind_cached_200_is_unpaused_once_and_confirmed(self):
        v = FakeVercel(["prj_a"])
        targets = [T("a", "prj_a"), T("a2", "prj_a")]
        lines, down = sentinel.run(targets, v, prober=lambda t: (True, "status 200"), wait=0)
        self.assertEqual(v.unpaused, ["prj_a"], "one unpause per project, not per domain")
        self.assertEqual(down, [])
        self.assertIn("- a: fixed (status 200).", lines)

    def test_unpause_that_does_not_take_is_still_down(self):
        v = FakeVercel(["prj_a"], takes=False)
        _, down = sentinel.run([T("a", "prj_a")], v, prober=lambda t: (True, "status 200"), wait=0)
        self.assertEqual(down, ["a"])

    def test_without_a_token_it_only_reports(self):
        _, down = sentinel.run([T("a", "prj_a")], None, prober=lambda t: (False, "status 503"), wait=0)
        self.assertEqual(down, ["a"])


if __name__ == "__main__":
    unittest.main()
