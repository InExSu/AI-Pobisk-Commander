"""opencode-rotate must record what the RUN did, not only what a probe said.

Until now the script wrote the shared health store from the probe path only.
The actual `opencode run` — the call that does the work — never reached the
store, so a model that answered every liveness probe and then failed the
task stayed "healthy" forever, and the fleet kept choosing it.

These tests drive the real script with a fake `opencode` binary: no network,
no credits, no account.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".agents", "skills", "opencode",
                      "opencode-rotate.sh")
MODEL = "opencode/test-model"


def make_env(tmp, body="echo 'done'; exit 0"):
    """One account with credentials, a fake opencode, and a stats store."""
    acc_root = os.path.join(tmp, "accounts")
    acct = os.path.join(acc_root, "acc1")
    os.makedirs(os.path.join(acct, "data", "opencode"), exist_ok=True)
    with open(os.path.join(acct, "data", "opencode", "auth.json"), "w",
              encoding="utf-8") as f:
        json.dump({"opencode": {"type": "oauth"}}, f)

    fake = os.path.join(tmp, "opencode")
    with open(fake, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n%s\n" % body)
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env.update({
        "OC_BIN": fake,
        "OC_ACCOUNTS_DIR": acc_root,
        "OC_MODELS": MODEL,
        "OC_NO_STATS": "0",
        "OC_STALL_WINDOW": "5",
        "OC_STALL_CHECK": "2",
        "AI_ROTATE_DIR": os.path.join(tmp, "rotate"),
    })
    return env


def run(env, extra=()):
    return subprocess.run([SCRIPT, "--account", "acc1", "--model", MODEL,
                           "--", "сделай работу"] + list(extra),
                          capture_output=True, text=True, env=env, timeout=60)


def store_of(tmp):
    p = os.path.join(tmp, "rotate", "model-stats.json")
    if not os.path.exists(p):
        return {}
    return json.load(open(p))


class TestRunIsRecorded(unittest.TestCase):
    def test_successful_run_records_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(tmp)
            p = run(env)
            store = store_of(tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(MODEL, store.get("opencode", {}))
        e = store["opencode"][MODEL]
        self.assertEqual(e["state"], "healthy")
        self.assertEqual(e["uptime"], [1, 1])

    def test_failed_run_records_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(tmp, body="echo 'boom' >&2; exit 3")
            p = run(env)
            store = store_of(tmp)
        self.assertEqual(p.returncode, 3)
        e = store.get("opencode", {}).get(MODEL)
        self.assertIsNotNone(e, "the run was not recorded at all")
        self.assertEqual(e["uptime"], [0, 1])
        self.assertEqual(e["fails"], 1)

    def test_rc_zero_with_a_dead_pattern_is_not_recorded_as_ok(self):
        # The provider printed a quota error but the process exited 0.
        # Recording `ok` here would teach the store that a broken model is a
        # healthy one.
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(tmp,
                           body="echo 'FreeUsageLimitError: daily limit'; exit 0")
            run(env)
            store = store_of(tmp)
        e = store.get("opencode", {}).get(MODEL)
        self.assertIsNotNone(e)
        self.assertNotEqual(e["state"], "healthy")
        self.assertEqual(e["uptime"], [0, 1])

    def test_quota_problem_is_a_limit_not_a_park(self):
        # An empty wallet is the ACCOUNT's problem. Parking the model for it
        # removed working models from rotation for a whole day.
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(tmp,
                           body="echo 'No payment method required'; exit 0")
            run(env)
            store = store_of(tmp)
        e = store.get("opencode", {}).get(MODEL)
        self.assertIsNotNone(e)
        self.assertEqual(e["state"], "ratelimit")
        self.assertNotEqual(e["state"], "auth_error")

    def test_broken_credential_still_parks(self):
        # The one case where parking is right: the key itself is dead.
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(tmp,
                           body="echo 'Invalid API key'; exit 0")
            run(env)
            store = store_of(tmp)
        e = store.get("opencode", {}).get(MODEL)
        self.assertIsNotNone(e)
        self.assertEqual(e["state"], "auth_error")


class TestAttribution(unittest.TestCase):
    def test_the_script_says_which_model_it_used(self):
        # Without this the journal's model column stays empty and a rotation
        # has no real starting point.
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(tmp)
            p = run(env)
        self.assertIn("pobisk-attr: model=%s account=acc1" % MODEL,
                      p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
