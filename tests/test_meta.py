"""Meta model parsing + the rules_only fallback that keeps ai_Pobisk alive."""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))
import meta as meta_mod
from outcome import Outcome, retry, rotate_model, rotate_account, fatal, loop_guard

CFG = {"meta": {"candidates": [], "timeout_sec": 30, "max_reparse": 1},
       "budget": {"max_meta_calls_per_task": 40, "max_retries_per_step": 3},
       "fallback": {"fatal_confidence_floor": 0.7}}


class TestParse(unittest.TestCase):
    def test_plain_json(self):
        v, c, _ = meta_mod.Meta._parse('{"verdict":"retry","confidence":0.9}')
        self.assertEqual((v, c), ("retry", 0.9))

    def test_fenced_json(self):
        v, _, _ = meta_mod.Meta._parse('```json\n{"verdict":"fatal"}\n```')
        self.assertEqual(v, "fatal")

    def test_json_surrounded_by_prose(self):
        v, _, _ = meta_mod.Meta._parse('Sure! {"verdict":"rotate_model"} Hope that helps')
        self.assertEqual(v, "rotate_model")

    def test_no_json(self):
        self.assertEqual(meta_mod.Meta._parse("no json")[0], None)

    def test_invalid_verdict_rejected(self):
        self.assertEqual(meta_mod.Meta._parse('{"verdict":"teleport"}')[0], None)

    def test_confidence_clamped(self):
        _, c, _ = meta_mod.Meta._parse('{"verdict":"retry","confidence":5}')
        self.assertEqual(c, 1.0)


class TestRulesFallback(unittest.TestCase):
    """No meta model -> deterministic rules. Degrade, never freeze."""
    def setUp(self):
        self.m = meta_mod.Meta(ROOT, CFG, {})
        self.m.model = None

    def test_retry_within_budget(self):
        v, c, r = self.m.decide(retry("s", "m", "a", "429"),
                                {"task": "t"}, {"retry": 0})
        self.assertEqual(v, "retry")

    def test_retry_exhausted_becomes_rotate_model(self):
        v, _, _ = self.m.decide(retry("s", "m", "a", "429"),
                                {"task": "t"}, {"retry": 5})
        self.assertEqual(v, "rotate_model")

    def test_rotate_model_passthrough(self):
        v, _, _ = self.m.decide(rotate_model("s"), {"task": "t"}, {})
        self.assertEqual(v, "rotate_model")

    def test_rotate_account_passthrough(self):
        v, _, _ = self.m.decide(rotate_account("s"), {"task": "t"}, {})
        self.assertEqual(v, "rotate_account")

    def test_loop_guard_is_fatal(self):
        v, _, _ = self.m.decide(loop_guard("s"), {"task": "t"}, {})
        self.assertEqual(v, "fatal")

    def test_unavailable_status(self):
        self.assertFalse(self.m.status()["available"])


class TestBudgetCap(unittest.TestCase):
    def test_stops_calling_after_cap(self):
        m = meta_mod.Meta(ROOT, CFG, {})
        m.model = ("nvidia", "m")
        m.calls = 999                     # over cap
        v, _, _ = m.decide(fatal("s"), {"task": "t"}, {})
        self.assertEqual(v, "fatal")      # came from rules, not a call
        self.assertEqual(m.calls, 999)


if __name__ == "__main__":
    unittest.main()
