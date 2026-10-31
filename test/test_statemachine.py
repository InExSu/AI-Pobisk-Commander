"""The control machine: sound graph, honest silhouette, correct rule order.

These are pure-logic tests — no network, no subprocess, no sleeps. The machine
is the one place where "what happens after a stop" is decided, so it is also
the cheapest place to catch a regression.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import statemachine as sm                       # noqa: E402
from outcome import Outcome                     # noqa: E402

BUDGETS = {
    "max_retries": 3, "max_model_rot": 10, "max_acct_rot": 5,
    "max_iters": 40, "max_nudges": 5, "fatal_floor": 0.7, "n_workers": 2,
}


def counters(**over):
    c = {"iters": 1, "worker_idx": 0, "retry": 0, "nudges": 0,
         "model_rot": 0, "acct_rot": 0}
    c.update(over)
    return c


def oc(name, detail=""):
    return Outcome(name, "fake", "m1", "acct", detail)


class TestGraph(unittest.TestCase):
    def test_machine_is_sound(self):
        self.assertEqual(sm.validate(), [])

    def test_silhouette_only_has_real_edges(self):
        # The drawing must not invent a path the code does not take.
        self.assertEqual(sm.silhouette_problems(), [])

    def test_every_terminal_reports_a_result(self):
        for state in sm.TERMINAL:
            self.assertIn(state, sm.RETURNS, "%s has no result string" % state)

    def test_initial_state_exists(self):
        self.assertIn(sm.INITIAL, sm.STATES)

    def test_no_terminal_has_exits(self):
        for state in sm.TERMINAL:
            self.assertEqual([t for f, _, t, _ in sm.TRANSITIONS if f == state],
                             [], "%s is terminal but has edges" % state)


class TestGuards(unittest.TestCase):
    def test_iteration_cap_stops(self):
        d = sm.guard(counters(iters=41), BUDGETS)
        self.assertEqual(d.state, sm.LOOP_ST)
        self.assertEqual(d.result, "loop_guard")

    def test_no_workers_left(self):
        d = sm.guard(counters(worker_idx=2), BUDGETS)
        self.assertEqual(d.state, sm.EXHAUSTED_ST)

    def test_fresh_counters_pass(self):
        self.assertIsNone(sm.guard(counters(), BUDGETS))


class TestRules(unittest.TestCase):
    def test_ok_short_circuits_before_anything_else(self):
        # Even with every budget blown, a successful step is a successful step.
        d = sm.decide(oc("ok"), counters(retry=99, nudges=99), BUDGETS)
        self.assertEqual(d.state, sm.OK_ST)

    def test_retry_below_budget_goes_to_meta(self):
        # The rule table does not retry on its own; the meta model decides.
        d = sm.decide(oc("retry"), counters(), BUDGETS)
        self.assertEqual(d.state, sm.META)

    def test_retry_over_budget_rotates_model(self):
        d = sm.decide(oc("retry"), counters(retry=3), BUDGETS)
        self.assertEqual(d.state, sm.ROTATE_MODEL_ST)
        self.assertEqual(d.source, "invariant")

    def test_account_failure_is_never_retried(self):
        d = sm.decide(oc("rotate_account"), counters(), BUDGETS)
        self.assertEqual(d.state, sm.ROTATE_ACCOUNT_ST)

    def test_capability_gap_rotates_account(self):
        d = sm.decide(oc("fatal", "I have no file access in this sandbox"),
                      counters(), BUDGETS)
        self.assertEqual(d.state, sm.ROTATE_ACCOUNT_ST)

    def test_waiting_becomes_a_nudge(self):
        d = sm.decide(oc("waiting", "Press Enter to continue"), counters(),
                      BUDGETS)
        self.assertEqual(d.state, sm.NUDGE)

    def test_nudge_loop_is_capped(self):
        d = sm.decide(oc("waiting"), counters(nudges=5), BUDGETS)
        self.assertEqual(d.state, sm.LOOP_ST)

    def test_question_is_not_a_meta_question(self):
        d = sm.decide(oc("question", "which DB?"), counters(), BUDGETS)
        self.assertEqual(d.state, sm.QUESTION_ST)
        self.assertEqual(d.source, "rules")

    def test_loop_guard_stops_immediately(self):
        d = sm.decide(oc("loop_guard"), counters(), BUDGETS)
        self.assertEqual(d.state, sm.LOOP_ST)

    def test_unknown_outcome_goes_to_the_model(self):
        d = sm.decide(oc("fatal", "something nobody has seen before"),
                      counters(), BUDGETS)
        self.assertEqual(d.state, sm.META)


class TestMetaResolution(unittest.TestCase):
    def test_confident_fatal_stops(self):
        d = sm.resolve_meta("fatal", 0.95, counters(), BUDGETS)
        self.assertEqual(d.state, sm.FATAL_ST)
        self.assertEqual(d.result, "fatal")

    def test_unsure_fatal_tries_elsewhere(self):
        # A model that is not sure must not be allowed to stop the run.
        d = sm.resolve_meta("fatal", 0.2, counters(), BUDGETS)
        self.assertEqual(d.state, sm.ROTATE_ACCOUNT_ST)

    def test_retry_within_budget(self):
        d = sm.resolve_meta("retry", 1.0, counters(retry=1), BUDGETS)
        self.assertEqual(d.state, sm.RETRY_ST)

    def test_retry_at_budget_rotates(self):
        d = sm.resolve_meta("retry", 1.0, counters(retry=3), BUDGETS)
        self.assertEqual(d.state, sm.ROTATE_MODEL_ST)

    def test_rotate_model(self):
        self.assertEqual(sm.resolve_meta("rotate_model", 1.0, counters(),
                                         BUDGETS).state, sm.ROTATE_MODEL_ST)

    def test_rotate_account(self):
        self.assertEqual(sm.resolve_meta("rotate_account", 1.0, counters(),
                                         BUDGETS).state, sm.ROTATE_ACCOUNT_ST)

    def test_question(self):
        self.assertEqual(sm.resolve_meta("question", 1.0, counters(),
                                         BUDGETS).state, sm.QUESTION_ST)

    def test_unknown_verdict_does_not_guess(self):
        d = sm.resolve_meta("make_coffee", 1.0, counters(), BUDGETS)
        self.assertEqual(d.state, sm.FATAL_ST)


class TestValidatorIsNotVacuous(unittest.TestCase):
    """`drakon check` is only worth something if it can fail."""

    def _validate_with(self, transitions):
        orig = sm.TRANSITIONS
        sm.TRANSITIONS = transitions
        try:
            return sm.validate()
        finally:
            sm.TRANSITIONS = orig

    def test_catches_a_dead_end(self):
        t = [x for x in sm.TRANSITIONS if x[0] != sm.ROTATE_MODEL_ST]
        self.assertTrue(any("dead end" in p for p in self._validate_with(t)))

    def test_catches_an_unreachable_state(self):
        t = [x for x in sm.TRANSITIONS if x[2] != sm.NEEDS_OWNER_ST]
        self.assertTrue(any("unreachable" in p for p in self._validate_with(t)))

    def test_catches_a_terminal_with_an_exit(self):
        t = list(sm.TRANSITIONS) + [(sm.FATAL_ST, "x", sm.DISPATCH, "")]
        self.assertTrue(any("outgoing" in p for p in self._validate_with(t)))

    def test_catches_a_drawn_edge_the_machine_lacks(self):
        orig = dict(sm._BRANCH_EVENT)
        sm._BRANCH_EVENT["выдуманная ветвь?"] = ("evaluate", "nope", "ok")
        try:
            self.assertTrue(sm.silhouette_problems())
        finally:
            sm._BRANCH_EVENT.clear()
            sm._BRANCH_EVENT.update(orig)


class TestRendering(unittest.TestCase):
    def test_ascii_mentions_every_terminal(self):
        art = sm.ascii_art()
        for state in sm.TERMINAL:
            self.assertIn(state, art)

    def test_svg_is_wellformed_enough(self):
        svg = sm.svg()
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.rstrip().endswith("</svg>"))
        self.assertIn("dispatch", svg)

    def test_svg_writes_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = sm.write_svg(os.path.join(d, "sub", "m.svg"))
            self.assertTrue(os.path.exists(p))
            self.assertGreater(os.path.getsize(p), 1000)


if __name__ == "__main__":
    unittest.main()
