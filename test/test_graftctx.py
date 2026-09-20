"""Graft context is an accelerator: it must never be able to break a run."""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import graftctx as G


class TestNoGraphNoCrash(unittest.TestCase):
    """A missing graft/ or a missing binary yields "", never an exception."""
    def test_missing_dir_returns_empty(self):
        self.assertEqual(G.context_for("/nonexistent/repo/xyz", "anything"), "")

    def test_empty_query_returns_empty(self):
        self.assertEqual(G.ask(ROOT, ""), "")

    def test_has_graph_false_for_missing(self):
        self.assertFalse(G.has_graph("/nonexistent/repo/xyz"))

    def test_status_shape(self):
        s = G.status(ROOT)
        self.assertIn("installed", s)
        self.assertIn("graph", s)


class TestParsing(unittest.TestCase):
    """graft ask --json uses {title, pointer, snippet}; be tolerant of both."""
    def test_title_pointer_shape(self):
        import graftctx
        orig = graftctx._run
        payload = {"hits": [{"kind": "symbol", "title": "Supervisor · class",
                             "pointer": "src/supervisor.py:L33",
                             "snippet": "class Supervisor"}]}
        graftctx._run = lambda *a, **k: (0, __import__("json").dumps(payload), "")
        try:
            out = graftctx.ask("/tmp", "q")
        finally:
            graftctx._run = orig
        self.assertIn("Supervisor", out)
        self.assertIn("src/supervisor.py", out)

    def test_name_path_shape_also_works(self):
        import graftctx
        orig = graftctx._run
        payload = {"hits": [{"name": "run_step", "path": "src/x.py:L10"}]}
        graftctx._run = lambda *a, **k: (0, __import__("json").dumps(payload), "")
        try:
            out = graftctx.ask("/tmp", "q")
        finally:
            graftctx._run = orig
        self.assertIn("run_step", out)

    def test_bad_json_returns_empty(self):
        import graftctx
        orig = graftctx._run
        graftctx._run = lambda *a, **k: (0, "not json", "")
        try:
            self.assertEqual(graftctx.ask("/tmp", "q"), "")
        finally:
            graftctx._run = orig

    def test_nonzero_exit_returns_empty(self):
        import graftctx
        orig = graftctx._run
        graftctx._run = lambda *a, **k: (1, "", "boom")
        try:
            self.assertEqual(graftctx.ask("/tmp", "q"), "")
        finally:
            graftctx._run = orig


class TestTruncation(unittest.TestCase):
    def test_context_is_capped(self):
        self.assertLessEqual(G.MAX_CONTEXT_CHARS, 8000)


if __name__ == "__main__":
    unittest.main()
