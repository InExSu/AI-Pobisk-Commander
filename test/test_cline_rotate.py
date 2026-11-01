"""cline-rotate switches --data-dir between accounts instead of giving up.

The skill used to run one account and `exit` on any non-zero rc, so a dead
account ended the whole task while four other trees sat unused. These tests
drive the real script with a fake `cline` binary: no credits, no network.
"""
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".agents", "skills", "cline", "cline-rotate.sh")

ACCOUNTS = ["airegist01", "ivrabo", "consolidation20111"]


def make_env(tmp):
    """Three fake accounts, a fake cline, and a log of the --data-dir values."""
    acc_root = os.path.join(tmp, "accounts")
    for name in ACCOUNTS:
        d = os.path.join(acc_root, name, "settings")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "providers.json"), "w", encoding="utf-8") as f:
            f.write('{"providers": {"cline": {"settings": {"model": "m1",'
                    ' "auth": {"metadata": {"userInfo": {"email": "%s@x"}}}}}}}'
                    % name)

    log = os.path.join(tmp, "data-dirs.txt")
    fake = os.path.join(tmp, "cline")
    with open(fake, "w", encoding="utf-8") as f:
        f.write("""#!/bin/sh
prev=""
case " $* " in *" --auto-approve true "*) run=1 ;; *) run=0 ;; esac
for a in "$@"; do
  [ "$run" = 1 ] && [ "$prev" = "--data-dir" ] && echo "$a" >> "%s"
  prev="$a"
done
case " $* " in
  *" --auto-approve false "*) echo "ok"; exit 0 ;;
esac
%s
""" % (log, 'echo "Error: 402 credit insufficient"; exit 1'))
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env.update({
        "CLINE_BIN": fake,
        "CLINE_ACCOUNTS_DIR": acc_root,
        "CLINE_LOG_DIR": os.path.join(tmp, "logs"),
        "CLINE_MODELS": "m1",
        "CLINE_NO_STATS": "1",
        "CLINE_MAX_ACCOUNT_ROUNDS": str(len(ACCOUNTS)),
        "AI_ROTATE_DIR": os.path.join(tmp, "rotate"),
    })
    return env, log, fake


def run(env, prompt="сделай работу"):
    return subprocess.run([SCRIPT, prompt], capture_output=True, text=True,
                          env=env, timeout=120)


def tried(log):
    if not os.path.exists(log):
        return []
    with open(log, encoding="utf-8") as f:
        return [os.path.basename(l.strip()) for l in f if l.strip()]


class TestAccountSwitch(unittest.TestCase):
    def test_failure_moves_to_the_next_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log, _ = make_env(tmp)
            p = run(env)
            seen = tried(log)
        # Every account was tried, and the run ended with the skill's
        # "all accounts exhausted" code rather than a silent success.
        self.assertEqual(sorted(seen), sorted(ACCOUNTS))
        self.assertEqual(p.returncode, 5)

    def test_success_stops_at_the_first_working_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log, fake = make_env(tmp)
            # Fail on the first account only, then succeed.
            counter = os.path.join(tmp, "n")
            with open(fake, "w", encoding="utf-8") as f:
                f.write("""#!/bin/sh
prev=""
case " $* " in *" --auto-approve true "*) run=1 ;; *) run=0 ;; esac
for a in "$@"; do
  [ "$run" = 1 ] && [ "$prev" = "--data-dir" ] && echo "$a" >> "%s"
  prev="$a"
done
case " $* " in
  *" --auto-approve false "*) echo "ok"; exit 0 ;;
esac
n=$(cat "%s" 2>/dev/null || echo 0)
echo $((n+1)) > "%s"
if [ "$n" -ge 1 ]; then echo "Done: 3 files changed"; exit 0; fi
echo "Error: 402 credit insufficient"; exit 1
""" % (log, counter, counter))
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            p = run(env)
            seen = tried(log)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(len(seen), 2, "should stop after the second account: %s" % seen)

    def test_every_account_is_tried_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log, _ = make_env(tmp)
            run(env)
            seen = tried(log)
        self.assertEqual(len(seen), len(set(seen)), "account tried twice: %s" % seen)


class TestSilentModel(unittest.TestCase):
    """A model that stops answering must cost one model, not the whole step.

    Seen live: cline accepted the prompt, wrote nothing for ten minutes, and
    the supervisor sat inside one call until its 30-minute step timeout. The
    bound is cline's own --timeout — an external watchdog cannot be used here,
    because a `kill` from a background subshell is not allowed on every host.
    """

    def fake(self, tmp, log, body):
        fake = os.path.join(tmp, "cline")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n"
                    "prev=\"\"\n"
                    "case \" $* \" in *\" --auto-approve true \"*) run=1 ;; *) run=0 ;; esac\n"
                    "for a in \"$@\"; do\n"
                    "  [ \"$run\" = 1 ] && [ \"$prev\" = \"--data-dir\" ] && echo \"$a\" >> \"%s\"\n"
                    "  prev=\"$a\"\n"
                    "done\n"
                    "case \" $* \" in *\" --auto-approve false \"*) echo ok; exit 0 ;; esac\n"
                    "%s\n" % (log, body))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        return fake

    def test_run_timeout_is_passed_to_cline(self):
        # Without the flag the script has no bound at all; the whole point is
        # that the binary gives up by itself.
        with tempfile.TemporaryDirectory() as tmp:
            env, log, _ = make_env(tmp)
            args = os.path.join(tmp, "args.txt")
            fake = self.fake(tmp, log, 'echo "$@" > "%s"; echo "done"; exit 0' % args)
            env["CLINE_BIN"] = fake
            env["CLINE_RUN_TIMEOUT"] = "77"
            run(env)
            with open(args, encoding="utf-8") as f:
                argv = f.read()
        self.assertIn("--timeout 77", argv)

    def test_silent_worker_is_killed_by_the_stall_guard(self):
        # No output, no session files: the guard must end the call on its own.
        # Waiting for the supervisor's 30-minute step timeout is what made a
        # hung provider cost the whole run.
        with tempfile.TemporaryDirectory() as tmp:
            env, log, _ = make_env(tmp)
            fake = self.fake(tmp, log, "sleep 30")
            env["CLINE_BIN"] = fake
            env["CLINE_MODELS"] = "m1"
            env["CLINE_STALL_WINDOW"] = "2"
            env["CLINE_STALL_CHECK"] = "1"
            started = time.time()
            p = run(env)
            elapsed = time.time() - started
            with open(os.path.join(tmp, "logs", "cline-rotate.log"),
                      encoding="utf-8") as f:
                rlog = f.read()

        self.assertLess(elapsed, 25, "the guard did not fire: %.0fs" % elapsed)
        self.assertIn("stall", rlog)
        self.assertEqual(p.returncode, 5)

    def test_timeout_output_rotates_the_model_not_the_account(self):
        # The credential answered the probe a second ago; only the model is
        # mute. Switching accounts here would burn the fleet on one bad model.
        with tempfile.TemporaryDirectory() as tmp:
            env, log, _ = make_env(tmp)
            fake = self.fake(tmp, log, """
case " $* " in
  *" -m m1 "*) echo "Request timed out"; exit 1 ;;
esac
echo "done"; exit 0""")
            env["CLINE_BIN"] = fake
            env["CLINE_MODELS"] = "m1 m2"
            p = run(env)
            seen = tried(log)
            with open(os.path.join(tmp, "logs", "cline-rotate.log"),
                      encoding="utf-8") as f:
                rlog = f.read()

        self.assertIn("next model, same account", rlog)
        self.assertEqual(p.returncode, 0, "second model should have finished")
        # Two attempts, both on the same --data-dir: the model rotated, the
        # account did not.
        self.assertEqual(set(seen), {"airegist01"}, "account switched: %s" % seen)


class TestProbeDoesNotParkModels(unittest.TestCase):
    """A failed probe says very little about the MODEL.

    The probe used to record `auth` for any non-limit error, and a park bans
    a model for 24 hours. Eight of cline's thirteen models sat in auth_error
    that way — including one with fourteen recorded successes — and the whole
    fleet collapsed onto a single model. Every one of those parks was caused
    by a provider stream error or an empty balance, never by a broken
    credential.
    """

    def probe_fake(self, tmp, log, probe_body):
        fake = os.path.join(tmp, "cline")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("""#!/bin/sh
prev=""
case " $* " in *" --auto-approve true "*) run=1 ;; *) run=0 ;; esac
for a in "$@"; do
  [ "$run" = 1 ] && [ "$prev" = "--data-dir" ] && echo "$a" >> "%s"
  prev="$a"
done
case " $* " in
  *" --auto-approve false "*) %s ;;
esac
echo "Done: 3 files changed"; exit 0
""" % (log, probe_body))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        return fake

    def store_of(self, tmp):
        import json
        p = os.path.join(tmp, "rotate", "model-stats.json")
        if not os.path.exists(p):
            return {}
        return json.load(open(p))

    def test_provider_error_does_not_park_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log, _ = make_env(tmp)
            env.pop("CLINE_NO_STATS", None)   # the store must be written
            env["CLINE_MODELS"] = "m1 m2"
            # m1's probe dies with a provider stream error; m2's probe works.
            fake = self.probe_fake(tmp, log, """
case " $* " in
  *" -m m2 "*) echo ok; exit 0 ;;
esac
echo "Failed to create stream: inference request failed"; exit 1""")
            env["CLINE_BIN"] = fake
            p = run(env)
            store = self.store_of(tmp)
            seen = tried(log)

        e = store.get("cline", {}).get("m1")
        self.assertIsNotNone(e, "the failed probe was not recorded at all")
        self.assertNotEqual(e["state"], "auth_error",
                            "a provider stream error must not park a model")
        self.assertEqual(e["uptime"], [0, 1])
        # The account is still usable: the model rotated, the account stayed.
        self.assertEqual(p.returncode, 0)
        self.assertEqual(set(seen), {"airegist01"}, "account switched: %s" % seen)

    def test_broken_credential_still_skips_the_account(self):
        # The one case that should end the account: the key itself is dead.
        with tempfile.TemporaryDirectory() as tmp:
            env, log, _ = make_env(tmp)
            env.pop("CLINE_NO_STATS", None)
            env["CLINE_MODELS"] = "m1"
            fake = self.probe_fake(tmp, log,
                                   'echo "Error: 401 Unauthorized"; exit 1')
            env["CLINE_BIN"] = fake
            p = run(env)
            with open(os.path.join(tmp, "logs", "cline-rotate.log"),
                      encoding="utf-8") as f:
                rlog = f.read()

        self.assertEqual(p.returncode, 5)
        self.assertIn("unusable (auth/error), skipping", rlog)

    def test_the_script_says_which_model_it_used(self):
        # Without this the journal's model column stays empty and a rotation
        # has no real starting point.
        with tempfile.TemporaryDirectory() as tmp:
            env, log, _ = make_env(tmp)
            p = run(env)
        self.assertIn("pobisk-attr: model=m1 account=", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
