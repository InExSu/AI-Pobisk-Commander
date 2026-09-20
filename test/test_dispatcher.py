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
        tasks, note = dispatcher.load_tasks(d, CFG)
        self.assertEqual(tasks, [])
        self.assertIn("no task dir", note)

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
    """The repo's own configs/tasks must parse."""
    def test_repo_tasks_found(self):
        tasks, _ = dispatcher.load_tasks(ROOT, CFG)
        self.assertTrue(tasks, "no tasks in %s" % CFG["tasks"]["dir"])

    def test_every_task_non_empty(self):
        tasks, _ = dispatcher.load_tasks(ROOT, CFG)
        for t in tasks:
            self.assertTrue(t.body.strip(), "%s is empty" % t.name)

    def test_ordering_stable(self):
        tasks, _ = dispatcher.load_tasks(ROOT, CFG)
        names = [t.name for t in tasks]
        self.assertEqual(names, sorted(names, key=dispatcher._order_key))


if __name__ == "__main__":
    unittest.main()
