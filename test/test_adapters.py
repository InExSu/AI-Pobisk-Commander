"""Adapters: exit code 0 is not proof of work.

A bare chat completion (nvidia, tokenharbor) has no file tools. Asked to edit
files it answers "I don't have access to your filesystem" in polite prose and
exits 0. The adapter used to translate that straight into `ok`, so the task
was marked done having changed nothing. These tests pin the fix.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "adapters"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import nvidia as nvidia_mod            # noqa: E402
import tokenharbor as th_mod           # noqa: E402
import base as base_mod                 # noqa: E402
from outcome import Outcome             # noqa: E402


def _o(http, detail):
    return Outcome("retry", "stub", "m", "a", detail, http, 100, detail)


class TestIsRateLimit(unittest.TestCase):
    """Plan #4 — a provider throttle is transient, not a model fault.

    429/520/529 and explicit rate-limit/quota prose must score as a ratelimit
    so the adapter records a brief 'limit' cooldown; plain 5xx (500/502/503)
    must stay ordinary failures so a genuinely broken model still hits the
    hard-failure breaker.
    """
    def test_http_429(self):
        self.assertTrue(base_mod.Adapter._is_ratelimit(_o(429, "")))
    def test_http_520(self):
        self.assertTrue(base_mod.Adapter._is_ratelimit(_o(520, "")))
    def test_http_529(self):
        self.assertTrue(base_mod.Adapter._is_ratelimit(_o(529, "")))

    def test_prose_rate_limit(self):
        self.assertTrue(base_mod.Adapter._is_ratelimit(
            _o(None, "you have hit a rate limit")))
    def test_prose_ratelimit(self):
        self.assertTrue(base_mod.Adapter._is_ratelimit(_o(None, "RATELIMIT reached")))
    def test_prose_quota_exceeded(self):
        self.assertTrue(base_mod.Adapter._is_ratelimit(
            _o(None, "quota exceeded for today")))
    def test_prose_too_many_requests(self):
        self.assertTrue(base_mod.Adapter._is_ratelimit(
            _o(None, "429 too many requests")))

    def test_http_500_not_ratelimit(self):
        self.assertFalse(base_mod.Adapter._is_ratelimit(_o(500, "")))
    def test_http_502_not_ratelimit(self):
        self.assertFalse(base_mod.Adapter._is_ratelimit(_o(502, "")))
    def test_http_503_not_ratelimit(self):
        self.assertFalse(base_mod.Adapter._is_ratelimit(_o(503, "")))
    def test_plain_error_text_not_ratelimit(self):
        self.assertFalse(base_mod.Adapter._is_ratelimit(
            _o(None, "internal server error")))


def patched(mod, rc, out, err=""):
    orig = mod.run_cmd
    mod.run_cmd = lambda *a, **k: (rc, out, err)
    return orig


class TestBareCompletionCannotClaimSuccess(unittest.TestCase):
    def test_nvidia_polite_refusal_is_not_ok(self):
        a = nvidia_mod.NvidiaAdapter(ROOT)
        orig = patched(nvidia_mod, 0,
                       "I don't have access to your filesystem. You could add "
                       "the following code to i18n.js yourself...")
        try:
            o = a.run("переведи строки")
        finally:
            nvidia_mod.run_cmd = orig
        self.assertNotEqual(o.outcome, "ok")
        self.assertEqual(o.outcome, "rotate_account")

    def test_nvidia_real_answer_is_ok(self):
        a = nvidia_mod.NvidiaAdapter(ROOT)
        orig = patched(nvidia_mod, 0, "Готово: перевёл 42 строки.")
        try:
            o = a.run("переведи строки")
        finally:
            nvidia_mod.run_cmd = orig
        self.assertEqual(o.outcome, "ok")

    def test_tokenharbor_polite_refusal_is_not_ok(self):
        a = th_mod.TokenHarborAdapter(ROOT)
        orig = patched(th_mod, 0,
                       "I cannot access your local files, but here is a patch "
                       "you can apply manually.")
        try:
            o = a.run("поправь i18n.js")
        finally:
            th_mod.run_cmd = orig
        self.assertEqual(o.outcome, "rotate_account")

    def test_tokenharbor_real_answer_is_ok(self):
        a = th_mod.TokenHarborAdapter(ROOT)
        orig = patched(th_mod, 0, "Done.")
        try:
            o = a.run("скажи ok")
        finally:
            th_mod.run_cmd = orig
        self.assertEqual(o.outcome, "ok")


class TestExitCodes(unittest.TestCase):
    def test_nvidia_missing_key_is_fatal(self):
        a = nvidia_mod.NvidiaAdapter(ROOT)
        orig = patched(nvidia_mod, 2, "")
        try:
            self.assertEqual(a.run("x").outcome, "fatal")
        finally:
            nvidia_mod.run_cmd = orig

    def test_nvidia_all_models_dead_rotates_model(self):
        a = nvidia_mod.NvidiaAdapter(ROOT)
        orig = patched(nvidia_mod, 3, "")
        try:
            self.assertEqual(a.run("x").outcome, "rotate_model")
        finally:
            nvidia_mod.run_cmd = orig


if __name__ == "__main__":
    unittest.main()
