"""Task discovery: ordering, empty files, heading extraction."""
import os, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import dispatcher

CFG = {"tasks": {"dir": "configs/tasks", "skip_empty": True}}


def make_repo(files):
    d = tempfile.mkdtemp()
    td = os.path.join(d, "configs", "tasks")
    os.makedirs(td)
    for name, body in files.items():
        with open(os.path.join(td, name), "w") as f:
            f.write(body)
    return d


class TestLoad(unittest.TestCase):
    def test_ordered_by_leading_number(self):
        r = make_repo({"02 b.md": "B", "01 a.md": "A", "10 c.md": "C"})
        tasks, _ = dispatcher.load_tasks(r, CFG)
        self.assertEqual([t.name for t in tasks], ["01 a.md", "02 b.md", "10 c.md"])

    def test_empty_skipped_not_failed(self):
        r = make_repo({"01 a.md": "A", "02 empty.md": ""})
        tasks, skipped = dispatcher.load_tasks(r, CFG)
        self.assertEqual(len(tasks), 1)
        self.assertTrue(any("empty" in s for s in skipped))

    def test_whitespace_only_is_empty(self):
        r = make_repo({"01 a.md": "   \n\n  "})
        tasks, skipped = dispatcher.load_tasks(r, CFG)
        self.assertEqual(tasks, [])
        self.assertEqual(len(skipped), 1)

    def test_missing_dir_is_not_error(self):
        d = tempfile.mkdtemp()
        tasks, skipped = dispatcher.load_tasks(d, CFG)
        self.assertEqual(tasks, [])
        # skipped is a list of notes; a missing dir is ONE note, not a bare
        # string (a string made len(skipped) count characters of the path).
        self.assertEqual(len(skipped), 1)
        self.assertIn("no task dir", skipped[0])

    def test_heading_becomes_title(self):
        r = make_repo({"01 a.md": "# My Title\n\nbody text"})
        tasks, _ = dispatcher.load_tasks(r, CFG)
        self.assertEqual(tasks[0].title, "My Title")
        self.assertEqual(tasks[0].body.strip(), "body text")

    def test_one_step_per_task(self):
        r = make_repo({"01 a.md": "do this"})
        tasks, _ = dispatcher.load_tasks(r, CFG)
        self.assertEqual(tasks[0].steps(), ["do this"])


class TestRealTasks(unittest.TestCase):
    """Whatever .md files the repo currently ships must parse and order.

    The set may legitimately be empty: done tasks are archived into
    configs/disabled, so configs/tasks can end up with zero .md files —
    that is a normal state, not a broken discovery. Hence the guard, not
    a hard "at least one task" assert.
    """

    def _has_task_files(self):
        d = os.path.join(ROOT, CFG["tasks"]["dir"])
        return os.path.isdir(d) and any(
            n.lower().endswith(".md") for n in os.listdir(d))

    def test_repo_tasks_found(self):
        tasks = dispatcher.load_tasks(ROOT, CFG)[0]
        if not self._has_task_files():
            self.skipTest("configs/tasks holds no .md — tasks archived")
        self.assertTrue(tasks, "task .md files exist but none parsed")

    def test_every_task_non_empty(self):
        tasks = dispatcher.load_tasks(ROOT, CFG)[0]
        for t in tasks:
            self.assertTrue(t.body.strip(), "%s is empty" % t.name)

    def test_ordering_stable(self):
        tasks = dispatcher.load_tasks(ROOT, CFG)[0]
        names = [t.name for t in tasks]
        self.assertEqual(names, sorted(names, key=dispatcher._order_key))


if __name__ == "__main__":
    unittest.main()
