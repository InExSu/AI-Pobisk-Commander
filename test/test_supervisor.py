"""The recovery loop: what happens after each kind of stop."""
import os, sys, unittest, json, time, subprocess, tempfile, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))
import config, dispatcher, journal as J, meta as M, supervisor as S
from outcome import Outcome


def _git_repo():
    """A clean, committed git repo in a temp dir, or None if git is absent."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        return None
    d = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
        with open(os.path.join(d, "seed.txt"), "w") as f:
            f.write("seed\n")
        subprocess.run(["git", "-C", d, "add", "-A"], check=True)
        subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    except Exception:
        return None
    return d


def _new_supervisor(root, seq):
    j = J.Journal(root, CFG); j.reset()
    f = Fake(seq)
    m = M.Meta(root, CFG, {"fake": f}); m.model = None
    sv = S.Supervisor(root, CFG, {"fake": f}, m, j, verbose=False)
    sv.worker_order = ["fake"]
    return sv, f, j

CFG = {
    "meta": {"candidates": [], "timeout_sec": 30, "max_reparse": 0},
    "budget": {"max_retries_per_step": 3, "max_model_rotations": 10,
               "max_account_rotations": 5, "max_meta_calls_per_task": 40,
               "max_wall_clock_sec": 3600, "max_steps_per_task": 50,
               "step_timeout_sec": 60},
    "fallback": {"on_meta_unavailable": "rules_only", "fatal_confidence_floor": 0.7},
    "workers": {"order": ["fake"], "non_workers": []},
    "questions": {"auto_answer_from": [], "ask_owner_when_confidence_below": 0.6,
                  "max_pending": 3, "owner_timeout_sec": 600},
    "tasks": {"dir": "configs/tasks", "skip_empty": True},
    "state": {"dir": ".ai_pobisk_test", "journal": "j.md", "state_file": "s.json"},
}


class Fake:
    skill = "fake"
    script_path = "/bin/true"
    def __init__(self, seq):
        self.seq, self.i, self.seen = list(seq), 0, []
    def available(self): return True
    def models(self): return ["m1", "m2"]
    def run(self, prompt, model=None, account=None, timeout=None):
        o = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        self.seen.append(model)
        return Outcome(o, "fake", model or "m1", "acct", "sim")


def run_step(seq):
    j = J.Journal(ROOT, CFG); j.reset()
    f = Fake(seq)
    m = M.Meta(ROOT, CFG, {"fake": f}); m.model = None
    sv = S.Supervisor(ROOT, CFG, {"fake": f}, m, j, verbose=False)
    sv.worker_order = ["fake"]
    t = dispatcher.Task("/tmp/t.md", "t", "body", 1)
    res = sv.run_step(t, 1, "prompt")
    j.reset()
    return res, f


class TestRecovery(unittest.TestCase):
    def test_ok_first_try(self):
        self.assertEqual(run_step(["ok"])[0], "ok")

    def test_retry_then_ok(self):
        self.assertEqual(run_step(["retry", "ok"])[0], "ok")

    def test_model_rotation_switches_model(self):
        res, f = run_step(["rotate_model", "ok"])
        self.assertEqual(res, "ok")
        self.assertIn("m2", f.seen)      # actually moved to another model

    def test_account_rotation_exhausts(self):
        self.assertEqual(run_step(["rotate_account"] * 9)[0], "exhausted")

    def test_fatal_stops(self):
        self.assertEqual(run_step(["fatal"])[0], "fatal")

    def test_loop_guard_stops(self):
        self.assertEqual(run_step(["loop_guard"])[0], "loop_guard")

    def test_retry_budget_then_rotate_model(self):
        # 4 retries > cap 3 -> must rotate, not spin forever
        res, f = run_step(["retry"] * 4 + ["ok"])
        self.assertEqual(res, "ok")
        self.assertIn("m2", f.seen)


class TestNoInfiniteLoop(unittest.TestCase):
    def test_always_retry_terminates(self):
        # Pathological: the skill never recovers. Must still return.
        self.assertIn(run_step(["retry"])[0], ("ok", "exhausted", "fatal",
                                               "loop_guard"))


class RepoEditingFake(Fake):
    """A worker that actually touches the target repo before reporting ok —
    i.e. it did the work the evidence check is looking for."""

    def __init__(self, seq, repo):
        super().__init__(seq)
        self.repo = repo

    def run(self, prompt, model=None, account=None, timeout=None):
        with open(os.path.join(self.repo, "seed.txt"), "a") as fh:
            fh.write("edited by worker\n")
        return super().run(prompt, model=model, account=account, timeout=timeout)


class TestEvidence(unittest.TestCase):
    """A file step that reports ok but leaves the target repo unchanged is a
    false positive — a weak model that returned rc=0 having done nothing."""

    def test_ok_with_unchanged_repo_is_needs_review(self):
        repo = _git_repo()
        if repo is None:
            self.skipTest("git not available")
        root = tempfile.mkdtemp()
        sv, f, j = _new_supervisor(root, ["ok"])
        # trailing slash so _target_dir_from_task recognises the absolute dir
        # on platforms whose path regex requires it; names a real git repo so
        # the evidence check has a fingerprint to compare against.
        body = "edit %s/ and commit" % repo
        t = dispatcher.Task(os.path.join(root, "t.md"), "t", body, 1)
        res = sv.run_step(t, 1, "prompt")
        self.assertEqual(res, "needs_review")
        self.assertTrue(sv._no_evidence)

    def test_ok_with_changed_repo_is_ok(self):
        repo = _git_repo()
        if repo is None:
            self.skipTest("git not available")
        root = tempfile.mkdtemp()
        j = J.Journal(root, CFG); j.reset()
        f = RepoEditingFake(["ok"], repo)
        m = M.Meta(root, CFG, {"fake": f}); m.model = None
        sv = S.Supervisor(root, CFG, {"fake": f}, m, j, verbose=False)
        sv.worker_order = ["fake"]
        body = "edit %s/ and commit" % repo
        t = dispatcher.Task(os.path.join(root, "t.md"), "t", body, 1)
        res = sv.run_step(t, 1, "prompt")
        self.assertEqual(res, "ok")
        self.assertFalse(sv._no_evidence)


class TestStatus(unittest.TestCase):
    """status.json is the single file an outside observer reads; it must fold
    the worker heartbeat and never raise."""

    def test_write_merges_keys(self):
        d = tempfile.mkdtemp()
        st = S._Status(d)
        st.write(100.0, state="running", task="t", worker="fake")
        with open(st.path) as fh:
            cur = json.load(fh)
        self.assertEqual(cur["state"], "running")
        self.assertEqual(cur["task"], "t")
        self.assertEqual(cur["worker"], "fake")
        self.assertEqual(cur["last_activity"], 100.0)
        # a second write merges, not clobbers
        st.write(101.0, worker="cline")
        with open(st.path) as fh:
            cur = json.load(fh)
        self.assertEqual(cur["state"], "running")   # kept from before
        self.assertEqual(cur["worker"], "cline")    # updated
        self.assertEqual(cur["last_activity"], 101.0)

    def test_fold_and_clear_heartbeat(self):
        d = tempfile.mkdtemp()
        st = S._Status(d)
        hb = os.path.join(d, "worker_heartbeat.json")
        with open(hb, "w") as fh:
            json.dump({"last_active": 555.0}, fh)
        st.write(100.0, state="running")
        la = st.fold_and_clear_heartbeat(100.0)
        self.assertEqual(la, 555.0)                 # heartbeat wins
        self.assertFalse(os.path.exists(hb))         # beacon removed
        with open(st.path) as fh:
            cur = json.load(fh)
        self.assertEqual(cur["last_activity"], 555.0)


class TestAttemptJournaling(unittest.TestCase):
    """The journal must say WHICH attempt and WHICH model.

    `attempt_no` was permanently 0 — the counter key was never bumped — and
    `model` was permanently empty, because the adapters never learned which
    model actually served the call. Together they made the journal useless
    for answering "what happened on attempt 3?"
    """

    def _offset(self):
        p = os.path.join(ROOT, CFG["state"]["dir"], "events.jsonl")
        if not os.path.exists(p):
            return 0
        with open(p, encoding="utf-8") as f:
            return len(f.readlines())

    def _attempts(self, offset):
        p = os.path.join(ROOT, CFG["state"]["dir"], "events.jsonl")
        if not os.path.exists(p):
            return []
        out = []
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[offset:]:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "attempt":
                out.append(e)
        return out

    def _run(self, seq):
        offset = self._offset()
        j = J.Journal(ROOT, CFG)
        j.reset()
        f = Fake(seq)
        m = M.Meta(ROOT, CFG, {"fake": f})
        m.model = None
        sv = S.Supervisor(ROOT, CFG, {"fake": f}, m, j, verbose=False)
        sv.worker_order = ["fake"]
        t = dispatcher.Task("/tmp/t.md", "t", "body", 1)
        sv.run_step(t, 1, "prompt")
        return self._attempts(offset)

    def test_the_first_attempt_is_number_one(self):
        attempts = self._run(["ok"])
        self.assertTrue(attempts, "no attempt event was written")
        self.assertEqual(attempts[0]["attempt_no"], 1)

    def test_attempt_numbers_increase(self):
        attempts = self._run(["retry", "ok"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual([a["attempt_no"] for a in attempts], [1, 2])

    def test_the_model_is_recorded(self):
        attempts = self._run(["ok"])
        self.assertEqual(attempts[0]["model"], "m1")

    def test_the_model_the_worker_reported_wins(self):
        # A worker that rotates internally reports the model that served;
        # the journal must record that, not the empty pinned value.
        class Reporting(Fake):
            def run(self, prompt, model=None, account=None, timeout=None):
                o = self.seq[min(self.i, len(self.seq) - 1)]
                self.i += 1
                return Outcome(o, "fake", "real/model", "acct", "sim")

        offset = self._offset()
        j = J.Journal(ROOT, CFG)
        j.reset()
        f = Reporting(["ok"])
        m = M.Meta(ROOT, CFG, {"fake": f})
        m.model = None
        sv = S.Supervisor(ROOT, CFG, {"fake": f}, m, j, verbose=False)
        sv.worker_order = ["fake"]
        t = dispatcher.Task("/tmp/t.md", "t", "body", 1)
        sv.run_step(t, 1, "prompt")
        attempts = self._attempts(offset)
        self.assertEqual(attempts[0]["model"], "real/model")


class TestDurability(unittest.TestCase):
    """Pick up after a run that died mid-step: a frozen status.json and a
    stale heartbeat beacon left by a corpse must not fool a fresh run."""

    def _events(self, root):
        p = os.path.join(root, CFG["state"]["dir"], "events.jsonl")
        if not os.path.exists(p):
            return []
        out = []
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        return out

    def test_recover_removes_heartbeat_and_logs_event(self):
        root = tempfile.mkdtemp()
        sv, f, j = _new_supervisor(root, ["ok"])
        sd = sv.status_dir
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "worker_heartbeat.json"), "w") as fh:
            json.dump({"last_active": time.time()}, fh)
        with open(os.path.join(sd, "status.json"), "w") as fh:
            json.dump({"state": "running", "run_pid": 999999,
                       "current_step": 3, "task": "t"}, fh)
        sv._recover_if_crashed("t")
        self.assertFalse(
            os.path.exists(os.path.join(sd, "worker_heartbeat.json")),
            "stale heartbeat from a dead run must be removed")
        self.assertTrue(
            any(e.get("type") == "recover" for e in self._events(root)),
            "a frozen 'running' status must log a recover event")


if __name__ == "__main__":
    unittest.main()
