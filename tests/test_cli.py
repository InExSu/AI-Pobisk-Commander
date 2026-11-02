"""The CLI contract: every command the README promises actually runs.

These are subprocess tests against `src/main.py` with the repo as the root.
They are read-only on purpose — nothing here starts a model, so the suite stays
offline and finishes in seconds.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "src", "main.py")


def run(*argv, timeout=90):
    env = dict(os.environ)
    env["AI_POBISK_ROOT"] = ROOT
    env.pop("PYTHONPATH", None)
    p = subprocess.run([sys.executable, MAIN] + list(argv),
                       capture_output=True, text=True, timeout=timeout,
                       cwd=ROOT, env=env)
    return p.returncode, p.stdout, p.stderr


class TestReadOnlyCommands(unittest.TestCase):
    def test_list(self):
        rc, out, _ = run("list")
        self.assertEqual(rc, 0)
        # The repo's configs/tasks can legitimately be empty: done tasks are
        # archived into configs/disabled. An honest listing ("tasks:") and an
        # honest note why there is nothing ("skipped: no task dir") both
        # satisfy the contract; empty output does not.
        self.assertTrue(
            ("tasks:" in out) or ("skipped:" in out),
            "list printed nothing meaningful:\n%s" % out)

    def test_list_alias_from_plan(self):
        # План.md spells it --list; both spellings must work.
        self.assertEqual(run("--list")[1], run("list")[1])

    def test_status_json_is_parseable(self):
        rc, out, _ = run("status", "--json")
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertIn("tasks", d)
        self.assertIn("state_file", d)

    def test_status_live_json(self):
        rc, out, _ = run("status", "--live", "--json")
        self.assertEqual(rc, 0)
        d = json.loads(out)
        for key in ("events", "tasks", "outcomes", "skills", "p95"):
            self.assertIn(key, d)

    def test_tail_does_not_crash_on_empty_history(self):
        rc, out, _ = run("tail", "--lines", "3")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip())

    def test_check_exit_code_is_zero_or_one(self):
        rc, out, _ = run("check")
        self.assertIn(rc, (0, 1))
        self.assertTrue(out.strip())


class TestReport(unittest.TestCase):
    def test_unknown_run_exits_one(self):
        rc, out, _ = run("report", "--run", "1999-01-01T00:00:00")
        self.assertEqual(rc, 1)
        self.assertIn("no such run", out)

    def test_latest_run_report(self):
        rc, out, _ = run("report")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip())


class TestDrakon(unittest.TestCase):
    def test_check_passes(self):
        rc, out, _ = run("drakon", "check")
        self.assertEqual(rc, 0, out)
        self.assertIn("ok —", out)

    def test_ascii_lists_the_loop(self):
        rc, out, _ = run("drakon")
        self.assertEqual(rc, 0)
        for word in ("dispatch", "loop_guard", "fatal", "needs_owner"):
            self.assertIn(word, out)

    def test_svg_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "machine.svg")
            rc, out, _ = run("drakon", "--svg", path)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                self.assertIn("<svg", f.read())


class TestGraft(unittest.TestCase):
    def test_status_is_honest_about_each_part(self):
        rc, out, _ = run("graft")
        # Separate lines for CLI, graph and whether it is used in prompts —
        # "graph: built" next to a missing CLI was the confusing case.
        self.assertIn("graft CLI:", out)
        self.assertIn("graph here:", out)
        self.assertIn("in prompts:", out)
        self.assertIn("repo:", out)


if __name__ == "__main__":
    unittest.main()
