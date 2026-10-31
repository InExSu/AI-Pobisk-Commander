"""Task files as instructions: nothing above the heading is dropped, and an
edited task is not "already done".

Both rules come from one afternoon of real use. A task whose first line was
the target repo path lost that path to the title splitter, so the worker was
pointed at the wrong repository. And a rewritten task was reported "done" in
one second, because state.json remembered the old step under the same file
name.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import dispatcher                 # noqa: E402
import journal as journal_mod     # noqa: E402
import supervisor                 # noqa: E402

CFG = {"state": {"dir": ".ai_pobisk_test", "journal": "j.md",
                 "state_file": "s.json"},
       "tasks": {"dir": "configs/tasks", "skip_empty": True}}


def task_from(raw, name="Иссуес.md"):
    title, body = dispatcher._split(raw, name)
    return dispatcher.Task(name, title, body, 1)


class TestSplit(unittest.TestCase):
    def test_heading_becomes_the_title(self):
        t = task_from("# Заголовок\n\nтело инструкции\n")
        self.assertEqual(t.title, "Заголовок")
        self.assertEqual(t.body.strip(), "тело инструкции")

    def test_text_above_the_heading_is_kept(self):
        # The regression: this preamble used to be dropped as "just a label".
        t = task_from("/abs/path/repo/\n\n# Заголовок\n\nтело\n")
        self.assertIn("/abs/path/repo/", t.body)
        self.assertIn("тело", t.body)
        self.assertEqual(t.title, "Заголовок")

    def test_no_heading_keeps_everything(self):
        raw = "первая строка\nвторая строка\n"
        t = task_from(raw)
        self.assertEqual(t.title, "Иссуес.md")
        self.assertEqual(t.body, raw)

    def test_target_dir_is_found_even_above_the_heading(self):
        # The failure this prevents: worker runs in ai_Pobisk instead of the
        # repository the task names.
        raw = "%s\n\n# Заголовок\n\nтело\n" % ROOT
        t = task_from(raw)
        self.assertEqual(supervisor._target_dir_from_task(t).rstrip("/"),
                         ROOT.rstrip("/"))

    def test_body_hash_changes_with_the_instruction(self):
        a = task_from("# T\n\nстарая инструкция\n")
        b = task_from("# T\n\nновая инструкция\n")
        self.assertNotEqual(a.body_hash, b.body_hash)

    def test_body_hash_ignores_trailing_whitespace(self):
        a = task_from("# T\n\nинструкция\n")
        b = task_from("# T\n\nинструкция\n\n\n")
        self.assertEqual(a.body_hash, b.body_hash)


class TestStateReconciliation(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.j = journal_mod.Journal(self.root, CFG)

    def test_first_run_records_the_hash(self):
        t = task_from("# T\n\nинструкция\n")
        st = supervisor._reconcile_task_state(self.j, t)
        self.assertEqual(st["body_hash"], t.body_hash)

    def test_unchanged_task_keeps_its_step(self):
        t = task_from("# T\n\nинструкция\n")
        supervisor._reconcile_task_state(self.j, t)
        self.j.mark(t.name, "done", 1)
        st = supervisor._reconcile_task_state(self.j, t)
        self.assertEqual(st["step"], 1)
        self.assertEqual(st["status"], "done")

    def test_edited_task_restarts_from_zero(self):
        t = task_from("# T\n\nстарая инструкция\n")
        supervisor._reconcile_task_state(self.j, t)
        self.j.mark(t.name, "done", 1)

        edited = task_from("# T\n\nновая инструкция\n")
        st = supervisor._reconcile_task_state(self.j, edited)
        self.assertEqual(st["step"], 0)
        self.assertEqual(st["status"], "pending")
        self.assertTrue(any("body changed" in n for n in st["notes"]))

    def test_state_survives_a_reload(self):
        t = task_from("# T\n\nинструкция\n")
        supervisor._reconcile_task_state(self.j, t)
        again = journal_mod.Journal(self.root, CFG)
        st = again.task_state(t.name)
        self.assertEqual(st["body_hash"], t.body_hash)


if __name__ == "__main__":
    unittest.main()
