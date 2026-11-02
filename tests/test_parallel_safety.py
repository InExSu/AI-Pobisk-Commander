"""Parallel-run safety: three failures from one live run, 2026-11-01.

A run of three tasks (all aimed at igrasu.ru) went parallel and two of the
three died instantly with

    [Errno 2] No such file or directory: '...state.json.tmp' -> '...state.json'

Three distinct causes, each with a test:

1. Journal.save() wrote a fixed tmp path; two threads raced on it and one
   os.replace found the file already gone.
2. _target_dir_from_task(".../igrasu.ru/") and (".../igrasu.ru/AGENTS.md")
   returned DIFFERENT strings (".../igrasu.ru/" vs ".../igrasu.ru"), so the
   same_repo_guard compared two unequal strings and allowed parallelism.
3. run_step exported PB_TARGET_DIR into the process environment; two threads
   running different tasks clobbered each other's target dir.
"""
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "adapters"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import base as adapter_base     # noqa: E402
import journal as journal_mod   # noqa: E402
import supervisor               # noqa: E402

CFG = {"state": {"dir": ".ai_pobisk_test", "journal": "j.md",
                 "state_file": "s.json"},
       "tasks": {"dir": "configs/tasks", "skip_empty": True}}


class _T:
    def __init__(self, name, body):
        self.name, self.body = name, body


class TestJournalThreadSafety(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.j = journal_mod.Journal(self.root, CFG)

    def test_tmp_name_is_unique_per_thread(self):
        # Two writers must never share one tmp path: the loser of the race
        # gets FileNotFoundError on os.replace, which is what killed tasks
        # 02 and 03 in the live run.
        seen = []
        orig = os.replace

        def spy(src, dst):
            seen.append(src)
            return orig(src, dst)

        other_name = []

        def worker():
            os.replace = spy
            try:
                self.j.mark("t2", "running")
            finally:
                os.replace = orig

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        os.replace = spy
        try:
            self.j.mark("t1", "running")
        finally:
            os.replace = orig

        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1],
                            "two threads wrote the same tmp path")

    def test_concurrent_saves_do_not_crash(self):
        # Stress the exact interleaving from the live run: two tasks marking
        # state at the same moment. The old fixed-tmp code loses this race
        # (FileNotFoundError from os.replace) within a few rounds.
        errors = []
        rounds = 60
        b = threading.Barrier(2)

        def worker(name):
            try:
                for i in range(rounds):
                    b.wait(timeout=10)
                    self.j.mark(name, "running", step=i % 5)
                    self.j.bump(name, "attempt")
            except Exception as e:      # noqa: BLE001 — the test IS the net
                errors.append(repr(e))

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])


class TestTargetDir(unittest.TestCase):
    def test_trailing_slash_and_file_path_agree(self):
        # The same_repo_guard compares dirs as strings; "repo/" and "repo"
        # are one repository but two strings, so the guard let three tasks
        # onto one repo at once. Normalization is the fix; this is the pin.
        d = tempfile.mkdtemp()
        repo = os.path.join(d, "igrasu.ru")
        os.makedirs(repo)
        slash = supervisor._target_dir_from_task(
            _T("a", "%s/\nулучшай" % repo))
        from_file = supervisor._target_dir_from_task(
            _T("b", "Прочитать %s/AGENTS.md и делать" % repo))
        self.assertTrue(slash)
        self.assertEqual(slash, from_file)
        self.assertFalse(slash.endswith(os.sep))


class TestRunCmdTargetDir(unittest.TestCase):
    def test_thread_local_target_wins_over_env(self):
        # Threads must not share one PB_TARGET_DIR: task A's export would
        # point task B's worker at A's repo. run_cmd reads a thread-local
        # target that the supervisor sets per step, env stays a fallback.
        a = tempfile.mkdtemp()
        b = tempfile.mkdtemp()
        old = os.environ.get("PB_TARGET_DIR")
        os.environ["PB_TARGET_DIR"] = a
        try:
            adapter_base.set_target_dir(b)
            rc, out, err = adapter_base.run_cmd(["/bin/pwd"], timeout=30)
            self.assertEqual(rc, 0, err)
            self.assertEqual(os.path.realpath(out.strip()),
                             os.path.realpath(b))
        finally:
            adapter_base.set_target_dir(None)
            if old is None:
                os.environ.pop("PB_TARGET_DIR", None)
            else:
                os.environ["PB_TARGET_DIR"] = old

    def test_env_fallback_still_works(self):
        a = tempfile.mkdtemp()
        old = os.environ.get("PB_TARGET_DIR")
        os.environ["PB_TARGET_DIR"] = a
        try:
            adapter_base.set_target_dir(None)
            rc, out, err = adapter_base.run_cmd(["/bin/pwd"], timeout=30)
            self.assertEqual(rc, 0, err)
            self.assertEqual(os.path.realpath(out.strip()),
                             os.path.realpath(a))
        finally:
            adapter_base.set_target_dir(None)
            if old is None:
                os.environ.pop("PB_TARGET_DIR", None)
            else:
                os.environ["PB_TARGET_DIR"] = old


if __name__ == "__main__":
    unittest.main()
