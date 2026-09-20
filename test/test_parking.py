"""Parking must be finite and precise, or rotation collapses silently.

Regression: 19 of cline's 20 models ended up parked permanently because any
account-level failure (quota, timeout, unknown) was recorded as `auth`.
"""
import importlib.util, json, os, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MS = os.path.join(ROOT, ".agents", "skills", "_shared", "model-stats.py")


def load_ms(tmpdir):
    os.environ["AI_ROTATE_DIR"] = tmpdir
    spec = importlib.util.spec_from_file_location("ms_%d" % time.time_ns(), MS)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestParkingIsFinite(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ms = load_ms(self.d)

    def tearDown(self):
        os.environ.pop("AI_ROTATE_DIR", None)

    def test_park_sets_expiry_not_forever(self):
        self.ms.cmd_record(["t", "m1", "auth", "0", "bad key"])
        e = self.ms.load()["t"]["m1"]
        self.assertEqual(e["state"], "auth_error")
        self.assertGreater(e["cooldown_until"], time.time())
        self.assertLessEqual(
            e["cooldown_until"] - time.time(), self.ms.AUTH_PARK_SEC + 5)

    def _order(self, models):
        """cmd_order prints the list to stdout and returns an int code."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ms.cmd_order(["t", "--models"] + models)
        return [l.strip() for l in buf.getvalue().splitlines() if l.strip()]

    def test_parked_model_excluded_while_parked(self):
        self.ms.cmd_record(["t", "m1", "auth", "0", "bad key"])
        self.ms.cmd_record(["t", "m2", "ok", "50", ""])
        out = self._order(["m1", "m2"])
        self.assertNotIn("m1", out)
        self.assertIn("m2", out)

    def test_returns_after_park_expires(self):
        self.ms.cmd_record(["t", "m1", "auth", "0", "bad key"])
        p = self.ms.STORE
        d = json.load(open(p))
        d["t"]["m1"]["cooldown_until"] = time.time() - 10
        json.dump(d, open(p, "w"))
        self.assertIn("m1", self._order(["m1", "m2"]))

    def test_stops_after_max_parks(self):
        for _ in range(self.ms.AUTH_MAX_PARKS):
            self.ms.cmd_record(["t", "m1", "auth", "0", "bad key"])
        p = self.ms.STORE
        d = json.load(open(p))
        d["t"]["m1"]["cooldown_until"] = time.time() - 10
        json.dump(d, open(p, "w"))
        self.assertNotIn("m1", self._order(["m1", "m2"]))


class TestCredentialVsAccount(unittest.TestCase):
    """`auth` may mean ONLY 'this credential is broken'."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from adapters import base
        self.base = base

    def _o(self, outcome, detail="", http=None):
        from outcome import Outcome
        return Outcome(outcome, "s", "m", "a", detail, http, 10, detail)

    def test_credential_failure_is_auth(self):
        self.assertTrue(
            self.base.Adapter._is_credential_failure(
                self._o("rotate_account", "credential rejected", 401)))

    def test_quota_is_not_auth(self):
        """Daily cap spent: the model is fine, the account is empty."""
        self.assertFalse(
            self.base.Adapter._is_credential_failure(
                self._o("rotate_account", "wallet empty or daily credit cap spent")))

    def test_timeout_is_not_auth(self):
        self.assertFalse(
            self.base.Adapter._is_credential_failure(
                self._o("fatal", "slow or timed-out call")))

    def test_unknown_is_not_auth(self):
        self.assertFalse(
            self.base.Adapter._is_credential_failure(self._o("fatal", "что-то странное")))

    def test_no_filesystem_is_not_auth(self):
        self.assertFalse(
            self.base.Adapter._is_credential_failure(
                self._o("rotate_account", "no file access: bare completion")))


class TestSweep(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        import logrot
        self.logrot = logrot

    def test_drops_permanently_parked(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "model-stats.json")
        json.dump({"t": {"m1": {"parks": 5, "state": "auth_error",
                                "uptime": [0, 9], "updated": time.time()},
                         "m2": {"parks": 0, "state": "healthy",
                                "uptime": [3, 3], "updated": time.time()}}},
                  open(p, "w"))
        n = self.logrot.sweep_stats(p, max_parks=3)
        self.assertEqual(n, 1)
        self.assertNotIn("m1", json.load(open(p))["t"])

    def test_drops_stale_never_succeeded(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "model-stats.json")
        json.dump({"t": {"old": {"parks": 0, "uptime": [0, 4],
                                 "updated": time.time() - 30 * 86400}}},
                  open(p, "w"))
        self.assertEqual(self.logrot.sweep_stats(p), 1)

    def test_keeps_healthy(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "model-stats.json")
        json.dump({"t": {"m": {"parks": 0, "state": "healthy",
                               "uptime": [5, 5], "updated": time.time()}}},
                  open(p, "w"))
        self.assertEqual(self.logrot.sweep_stats(p), 0)


if __name__ == "__main__":
    unittest.main()
