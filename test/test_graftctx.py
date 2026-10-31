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


class TestHasGraph(unittest.TestCase):
    """A directory called `graft` is not a graph — the artefacts are."""

    def _repo(self, files):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        for rel in files:
            p = os.path.join(d, rel)
            if rel.endswith("/"):
                os.makedirs(p, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        return d

    def test_empty_dir_named_graft_is_not_a_graph(self):
        # This is the bug that made `graft status` print "graph: built" while
        # the CLI was missing entirely.
        self.assertFalse(G.has_graph(self._repo(["graft/"])))

    def test_wiring_json_counts(self):
        self.assertTrue(G.has_graph(self._repo(["graft/.graph/wiring.json"])))

    def test_index_md_counts(self):
        self.assertTrue(G.has_graph(self._repo(["graft/INDEX.md"])))


class TestQueryLanguage(unittest.TestCase):
    """`ask` indexes English; a Cyrillic question is a guaranteed miss."""

    def test_russian_is_not_latin(self):
        self.assertFalse(G.mostly_latin("как супервизор решает сменить модель"))

    def test_mixed_but_russian_is_not_latin(self):
        # Task files are Russian with a few Latin words sprinkled in — that
        # must still count as Russian.
        self.assertFalse(G.mostly_latin(
            "Для ускорения работы в проект интегрирован Graft."))

    def test_english_is_latin(self):
        self.assertTrue(G.mostly_latin("rotate model when the limit is reached"))

    def test_empty_is_not_latin(self):
        self.assertFalse(G.mostly_latin(""))

    def test_cyrillic_query_does_not_call_ask(self):
        calls = []
        orig = (G.available, G.has_graph, G.ask, G.repo_map)
        G.available = lambda: True
        G.has_graph = lambda root: True
        G.ask = lambda root, q, limit=8: calls.append(q) or "HITS"
        G.repo_map = lambda root: "MAP"
        try:
            block = G.context_for("/tmp", "как супервизор меняет модель")
        finally:
            G.available, G.has_graph, G.ask, G.repo_map = orig
        self.assertEqual(calls, [])          # no wasted subprocess
        self.assertIn("MAP", block)          # the map still comes through


class TestTruncation(unittest.TestCase):
    def test_context_is_capped(self):
        self.assertLessEqual(G.MAX_CONTEXT_CHARS, 8000)


if __name__ == "__main__":
    unittest.main()
