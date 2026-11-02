"""Finding hands: a cooldown is a pause, not a verdict.

The supervisor used to walk its worker list once and then declare the task
impossible, while most of the fleet sat in a 15-minute cooldown. These tests
pin the difference between "paused" and "broken".
"""
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import hands                      # noqa: E402
import config                     # noqa: E402
import dispatcher                 # noqa: E402
import journal as J               # noqa: E402
import meta as M                  # noqa: E402
import supervisor as S            # noqa: E402
from outcome import Outcome       # noqa: E402

CFG = {
    "meta": {"candidates": [], "timeout_sec": 30, "max_reparse": 0},
    "budget": {"max_retries_per_step": 3, "max_model_rotations": 10,
               "max_account_rotations": 5, "max_meta_calls_per_task": 40,
               "max_wall_clock_sec": 3600, "max_steps_per_task": 50,
               "step_timeout_sec": 60, "max_rediscoveries": 1},
    "fallback": {"on_meta_unavailable": "rules_only",
                 "fatal_confidence_floor": 0.7},
    "workers": {"order": ["fake"], "non_workers": []},
    "questions": {"auto_answer_from": [], "ask_owner_when_confidence_below": 0.6,
                  "max_pending": 3, "owner_timeout_sec": 600},
    "tasks": {"dir": "configs/tasks", "skip_empty": True},
    "state": {"dir": ".ai_pobisk_test", "journal": "j.md", "state_file": "s.json"},
}


def store(entries):
    """A model-stats.json in a temp dir; returns its path."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "model-stats.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    return p


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class TestRevive(unittest.TestCase):
    def test_lifts_an_active_cooldown(self):
        now = time.time()
        p = store({"cline": {"m1": {"state": "down", "cooldown_until": now + 900,
                                    "fails": 3, "uptime": [2, 5]}}})
        out = hands.revive(p, ["cline"], now=now)
        self.assertEqual(out, {"cline": 1})
        e = load(p)["cline"]["m1"]
        self.assertEqual(e["cooldown_until"], 0)
        self.assertEqual(e["state"], "recovering")   # eligible, not trusted
        self.assertEqual(e["fails"], 3)              # history kept

    def test_never_lifts_an_auth_park(self):
        now = time.time()
        p = store({"cline": {"bad": {"state": "auth_error",
                                     "cooldown_until": now + 86400}}})
        self.assertEqual(hands.revive(p, ["cline"], now=now), {})
        self.assertEqual(load(p)["cline"]["bad"]["state"], "auth_error")

    def test_counts_models_that_are_already_eligible(self):
        now = time.time()
        p = store({"cline": {"m1": {"state": "recovering", "cooldown_until": 0}}})
        self.assertEqual(hands.revive(p, ["cline"], now=now), {"cline": 1})

    def test_ignores_other_skills(self):
        now = time.time()
        p = store({"cline": {"m1": {"state": "down", "cooldown_until": now + 60}},
                   "nvidia": {"m2": {"state": "down", "cooldown_until": now + 60}}})
        out = hands.revive(p, ["nvidia"], now=now)
        self.assertEqual(out, {"nvidia": 1})
        self.assertGreater(load(p)["cline"]["m1"]["cooldown_until"], 0)

    def test_missing_store_is_not_an_error(self):
        self.assertEqual(hands.revive("/nonexistent/x.json", ["cline"]), {})

    def test_broken_store_is_not_an_error(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "model-stats.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(hands.revive(p, ["cline"]), {})


class Fake:
    skill = "fake"
    script_path = "/bin/true"

    def __init__(self, seq):
        self.seq, self.i = list(seq), 0

    def available(self):
        return True

    def models(self):
        return ["m1"]

    def run(self, prompt, model=None, account=None, timeout=None):
        o = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return Outcome(o, "fake", model or "m1", "acct", "sim")


class TestSupervisorFindsHands(unittest.TestCase):
    def _supervisor(self, seq):
        j = J.Journal(ROOT, CFG)
        j.reset()
        f = Fake(seq)
        m = M.Meta(ROOT, CFG, {"fake": f})
        m.model = None
        sv = S.Supervisor(ROOT, CFG, {"fake": f}, m, j, verbose=False)
        sv.worker_order = ["fake"]
        return sv, j

    def test_find_hands_reports_what_it_revived(self):
        now = time.time()
        p = store({"fake": {"m1": {"state": "down", "cooldown_until": now + 900}}})
        orig = hands.HEALTH_STORE
        hands.HEALTH_STORE = p
        try:
            sv, j = self._supervisor(["ok"])
            self.assertEqual(sv._find_hands("t", 1), 1)
        finally:
            hands.HEALTH_STORE = orig
            j.reset()

    def test_dead_fleet_still_ends_in_exhausted(self):
        # Nothing to revive: the run must stop, not spin looking for hands.
        p = store({"fake": {"m1": {"state": "auth_error",
                                   "cooldown_until": time.time() + 86400}}})
        orig = hands.HEALTH_STORE
        hands.HEALTH_STORE = p
        try:
            sv, j = self._supervisor(["rotate_account"] * 9)
            t = dispatcher.Task("/tmp/t.md", "t", "body", 1)
            self.assertEqual(sv.run_step(t, 1, "prompt"), "exhausted")
        finally:
            hands.HEALTH_STORE = orig
            j.reset()


if __name__ == "__main__":
    unittest.main()
