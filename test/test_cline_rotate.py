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


if __name__ == "__main__":
    unittest.main()
