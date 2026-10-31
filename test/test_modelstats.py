"""model-stats.py: the shared health store and its ratelimit cooldown.

Plan item #4 — a provider throttle (HTTP 429/520/529, "rate limit", "quota")
is transient. The model is healthy; the provider is merely limiting. It must
be parked briefly (state "ratelimit", cooldown ~10 min) so the fleet routes
around a flaky provider, then retried — NOT counted toward the 15-minute
hard-failure breaker that would sit it out for the whole run.

The store ships as model-stats.py (hyphen), so it is loaded by path, exactly
as test_parking.py does.
"""
import importlib.util, os, sys, tempfile, time, unittest, contextlib, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MS = os.path.join(ROOT, ".agents", "skills", "_shared", "model-stats.py")


def load_ms(tmpdir):
    os.environ["AI_ROTATE_DIR"] = tmpdir
    spec = importlib.util.spec_from_file_location("ms_%d" % time.time_ns(), MS)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestRateLimitCooldown(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ms = load_ms(self.d)

    def tearDown(self):
        os.environ.pop("AI_ROTATE_DIR", None)

    def _order(self, skill, models):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ms.cmd_order([skill, "--models"] + models)
        return [m for m in buf.getvalue().split("\n") if m]

    def test_record_limit_parks_as_ratelimit(self):
        self.ms.cmd_record(["cline", "m1", "limit"])
        e = self.ms.load()["cline"]["m1"]
        self.assertEqual(e["state"], "ratelimit")
        # cooldown is roughly now + RATELIMIT_SEC (10 min), never the 15-min
        # hard-failure breaker.
        self.assertGreater(e["cooldown_until"], time.time())
        self.assertLessEqual(e["cooldown_until"],
                             time.time() + self.ms.RATELIMIT_SEC + 60)
        self.assertEqual(e["cooldown_until"],
                         e["updated"] + self.ms.RATELIMIT_SEC)

    def test_order_routes_around_ratelimit_while_cooling(self):
        # m1 throttled, m2 healthy -> m2 first, m1 only as last resort.
        self.ms.cmd_record(["cline", "m1", "limit"])
        self.ms.cmd_record(["cline", "m2", "ok"])
        self.assertEqual(self._order("cline", ["m1", "m2"]), ["m2", "m1"])

    def test_ratelimit_model_is_retried_after_cooldown(self):
        self.ms.cmd_record(["cline", "m1", "limit"])
        # Simulate the cooldown having passed.
        d = self.ms.load()
        d["cline"]["m1"]["cooldown_until"] = time.time() - 1
        self.ms.save(d)
        # After cooldown it is eligible again — cmd_order promotes it to a
        # one-shot "recovering" retry (in memory; it is not permanently parked).
        self.assertIn("m1", self._order("cline", ["m1"]))


if __name__ == "__main__":
    unittest.main()
