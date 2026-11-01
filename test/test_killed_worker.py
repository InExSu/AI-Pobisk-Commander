"""A worker that had to be KILLED is a model fault, not a transient.

One live run burned hours like this: opencode hung, the stall guard killed it
(rc -15), classify said "exit -15 with no recognised error" -> retry -> the
SAME model was hired for the same step and hung again — up to three times per
model, each round costing 5-30 minutes. A retry is for provider blips (5xx);
a worker that must be killed for silence gets a DIFFERENT model.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "adapters"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import base as adapter_base     # noqa: E402
import classify as classify_mod  # noqa: E402


class TestClassifyKilledWorker(unittest.TestCase):
    def test_signal_killed_worker_rotates_model(self):
        # rc < 0: the process was terminated by a signal (stall guard, watchdog).
        o = classify_mod.classify("step 3: reading files...", rc=-15,
                                  skill="opencode")
        self.assertEqual(o.outcome, "rotate_model")

    def test_timeout_killed_worker_rotates_model(self):
        # rc 124: run_cmd's own watchdog. The text also says "timed out" —
        # the prose rule alone maps it to retry, which repeats the hang.
        o = classify_mod.classify("nothing\ntimed out after 7200s", rc=124,
                                  skill="opencode")
        self.assertEqual(o.outcome, "rotate_model")

    def test_stall_guard_kill_rotates_model(self):
        o = classify_mod.classify("worker stalled, no output for 300s", rc=-15,
                                  skill="opencode")
        self.assertEqual(o.outcome, "rotate_model")

    def test_unrecognized_exit_1_does_not_rotate(self):
        # Not every failure is a kill: rc 1 with prose that matches nothing
        # must NOT rotate the model (base.run turns the unmatched exit into
        # a budget-capped retry; classify itself says ok).
        o = classify_mod.classify("something went wrong", rc=1, skill="opencode")
        self.assertIn(o.outcome, ("retry", "ok"))

    def test_provider_blip_still_retries(self):
        # A 5xx is the provider, not the model: retry remains right.
        o = classify_mod.classify("HTTP 503 overloaded", rc=1, skill="opencode")
        self.assertEqual(o.outcome, "retry")


class _StubAdapter(adapter_base.Adapter):
    skill = "stub"
    script = ""
    script_records_health = False

    def __init__(self, rc, out, err):
        super().__init__(os.path.dirname(os.path.abspath(__file__)))
        self._rc, self._out, self._err = rc, out, err

    def _invoke(self, prompt, model=None, account=None, timeout=None):
        return self._rc, self._out, self._err


class TestAdapterRunKilledWorker(unittest.TestCase):
    def test_run_maps_signal_kill_to_rotate_model(self):
        a = _StubAdapter(-15, "transcript line", "")
        o = a.run("do the step")
        self.assertEqual(o.outcome, "rotate_model")

    def test_run_maps_run_cmd_timeout_to_rotate_model(self):
        a = _StubAdapter(124, "partial output\n", "timed out after 7200s")
        o = a.run("do the step")
        self.assertEqual(o.outcome, "rotate_model")

    def test_run_unrecognized_exit_1_still_retries(self):
        # The conversion "rc!=0 but classify said ok -> retry" must survive:
        # only KILLED workers rotate, an ordinary failure keeps its retry.
        a = _StubAdapter(1, "something went wrong", "")
        o = a.run("do the step")
        self.assertEqual(o.outcome, "retry")


if __name__ == "__main__":
    unittest.main()
