"""The recovery loop: what happens after each kind of stop."""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))
import config, dispatcher, journal as J, meta as M, supervisor as S
from outcome import Outcome

CFG = {
    "meta": {"candidates": [], "timeout_sec": 30, "max_reparse": 0},
    "budget": {"max_retries_per_step": 3, "max_model_rotations": 10,
               "max_account_rotations": 5, "max_meta_calls_per_task": 40,
               "max_wall_clock_sec": 3600, "max_steps_per_task": 50,
               "step_timeout_sec": 60},
    "fallback": {"on_meta_unavailable": "rules_only", "fatal_confidence_floor": 0.7},
    "workers": {"order": ["fake"], "non_workers": []},
    "questions": {"auto_answer_from": [], "ask_owner_when_confidence_below": 0.6,
                  "max_pending": 3, "owner_timeout_sec": 600},
    "tasks": {"dir": "configs/tasks", "skip_empty": True},
    "state": {"dir": ".ai_pobisk_test", "journal": "j.md", "state_file": "s.json"},
}


class Fake:
    skill = "fake"
    script_path = "/bin/true"
    def __init__(self, seq):
        self.seq, self.i, self.seen = list(seq), 0, []
    def available(self): return True
    def models(self): return ["m1", "m2"]
    def run(self, prompt, model=None, account=None, timeout=None):
        o = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        self.seen.append(model)
        return Outcome(o, "fake", model or "m1", "acct", "sim")


def run_step(seq):
    j = J.Journal(ROOT, CFG); j.reset()
    f = Fake(seq)
    m = M.Meta(ROOT, CFG, {"fake": f}); m.model = None
    sv = S.Supervisor(ROOT, CFG, {"fake": f}, m, j, verbose=False)
    sv.worker_order = ["fake"]
    t = dispatcher.Task("/tmp/t.md", "t", "body", 1)
    res = sv.run_step(t, 1, "prompt")
    j.reset()
    return res, f


class TestRecovery(unittest.TestCase):
    def test_ok_first_try(self):
        self.assertEqual(run_step(["ok"])[0], "ok")

    def test_retry_then_ok(self):
        self.assertEqual(run_step(["retry", "ok"])[0], "ok")

    def test_model_rotation_switches_model(self):
        res, f = run_step(["rotate_model", "ok"])
        self.assertEqual(res, "ok")
        self.assertIn("m2", f.seen)      # actually moved to another model

    def test_account_rotation_exhausts(self):
        self.assertEqual(run_step(["rotate_account"] * 9)[0], "exhausted")

    def test_fatal_stops(self):
        self.assertEqual(run_step(["fatal"])[0], "fatal")

    def test_loop_guard_stops(self):
        self.assertEqual(run_step(["loop_guard"])[0], "loop_guard")

    def test_retry_budget_then_rotate_model(self):
        # 4 retries > cap 3 -> must rotate, not spin forever
        res, f = run_step(["retry"] * 4 + ["ok"])
        self.assertEqual(res, "ok")
        self.assertIn("m2", f.seen)


class TestNoInfiniteLoop(unittest.TestCase):
    def test_always_retry_terminates(self):
        # Pathological: the skill never recovers. Must still return.
        self.assertIn(run_step(["retry"])[0], ("ok", "exhausted", "fatal",
                                               "loop_guard"))


if __name__ == "__main__":
    unittest.main()
