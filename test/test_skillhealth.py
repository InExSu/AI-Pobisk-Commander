"""Skill health view: reads the store, never pings unless asked."""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import config
import main as ai_main


class FakeAdapter:
    def __init__(self, skill, available=True, models=()):
        self.skill, self._av, self._models = skill, available, list(models)
    def available(self): return self._av
    def models(self): return list(self._models)
    def run(self, prompt, model=None, account=None, timeout=None):
        from outcome import ok
        return ok(self.skill, model or "", "", "ok", 10, "ok")


class TestSkillHealth(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = config.load(ROOT)

    def test_missing_store_is_total(self):
        """A missing/health store must not crash the view."""
        import tempfile
        d = tempfile.mkdtemp()
        os.environ["AI_ROTATE_DIR"] = d      # empty dir: no store file
        try:
            adapters = {"cline": FakeAdapter("cline")}
            h = ai_main._skill_health(ROOT, self.cfg, adapters)
            self.assertIn("cline", h)
            self.assertEqual(h["cline"]["models"], 0)
            self.assertEqual(h["cline"]["calls"], 0)
        finally:
            os.environ.pop("AI_ROTATE_DIR", None)

    def test_counts_states(self):
        import json, tempfile, time
        d = tempfile.mkdtemp()
        os.environ["AI_ROTATE_DIR"] = d
        try:
            store = os.path.join(d, "model-stats.json")
            json.dump({"cline": {
                "m-ok": {"state": "healthy", "uptime": [3, 4], "p95": 1500,
                         "cooldown_until": 0},
                "m-auth": {"state": "auth_error", "uptime": [0, 1], "p95": 0,
                           "cooldown_until": 0},
                "m-cool": {"state": "down", "uptime": [0, 5], "p95": 0,
                           "cooldown_until": time.time() + 600},
            }}, open(store, "w"))
            h = ai_main._skill_health(ROOT, self.cfg,
                                      {"cline": FakeAdapter("cline")})
            c = h["cline"]
            self.assertEqual(c["models"], 3)
            self.assertEqual(c["live"], 1)
            self.assertEqual(c["auth"], 1)
            self.assertEqual(c["cooling"], 1)
            self.assertEqual(c["calls"], 10)
            self.assertEqual(c["p95"], 1500)
        finally:
            os.environ.pop("AI_ROTATE_DIR", None)

    def test_role_from_config(self):
        adapters = {"freebuff": FakeAdapter("freebuff")}
        h = ai_main._skill_health("/nonexistent/x", self.cfg, adapters)
        self.assertEqual(h["freebuff"]["role"], "capacity probe")


if __name__ == "__main__":
    unittest.main()
