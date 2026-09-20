"""Monitoring must be pure, total, and never able to break a run."""
import json, os, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import config, metrics


def ev(**kw):
    base = {"ts": 1.0, "run": "r1", "type": "attempt"}
    base.update(kw)
    return base


class TestSummarize(unittest.TestCase):
    def test_empty_is_total(self):
        """Every key the readers touch must exist, even with no events."""
        s = metrics.summarize([])
        for k in ("events", "tasks", "outcomes", "skills", "attempts",
                  "retries", "nudges", "rotations", "questions",
                  "invariants", "p50", "p95", "wall_ms"):
            self.assertIn(k, s)

    def test_counts_outcomes(self):
        s = metrics.summarize([ev(outcome="ok", skill="cline"),
                               ev(outcome="retry", skill="cline")])
        self.assertEqual(s["outcomes"]["ok"], 1)
        self.assertEqual(s["outcomes"]["retry"], 1)
        self.assertEqual(s["attempts"], 2)

    def test_skill_ok_ratio(self):
        s = metrics.summarize([ev(outcome="ok", skill="a"),
                               ev(outcome="fail", skill="a")])
        self.assertEqual(s["skills"]["a"]["ok_pct"], 50.0)

    def test_task_status_from_task_end(self):
        s = metrics.summarize([ev(task="t", outcome="ok"),
                               {"ts": 2.0, "run": "r1", "type": "task_end",
                                "task": "t", "result": "done"}])
        self.assertEqual(s["tasks"]["t"]["status"], "done")

    def test_percentiles(self):
        s = metrics.summarize([ev(outcome="ok", elapsed_ms=1000),
                               ev(outcome="ok", elapsed_ms=9000)])
        self.assertGreater(s["p95"], s["p50"])


class TestThresholds(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = config.load(ROOT)

    def test_clean_run_no_issues(self):
        s = metrics.summarize([ev(outcome="ok"), ev(outcome="ok"),
                               ev(outcome="ok"), ev(outcome="ok"),
                               ev(outcome="ok")])
        self.assertEqual(metrics.check_thresholds(s, self.cfg), [])

    def test_fatal_share_flagged(self):
        s = metrics.summarize([ev(outcome="fatal") for _ in range(8)]
                              + [ev(outcome="ok") for _ in range(2)])
        issues = metrics.check_thresholds(s, self.cfg)
        self.assertTrue(any("fatal" in i for i in issues))

    def test_many_nudges_flagged(self):
        s = metrics.summarize([{"ts": 1.0, "run": "r", "type": "nudge"}
                               for _ in range(20)])
        issues = metrics.check_thresholds(s, self.cfg)
        self.assertTrue(any("подталкив" in i for i in issues))


class TestPrometheus(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = config.load(ROOT)

    def test_has_help_and_type(self):
        s = metrics.summarize([ev(outcome="ok", skill="cline")])
        out = metrics.prometheus(s, self.cfg)
        self.assertIn("# HELP", out)
        self.assertIn("# TYPE", out)
        self.assertIn("aipobisk_attempts_total 1", out)

    def test_labels_escaped(self):
        s = metrics.summarize([ev(outcome="ok", skill='we"ird', task='a"b')])
        out = metrics.prometheus(s, self.cfg)
        self.assertIn('\\"', out)


class TestMetricsIO(unittest.TestCase):
    def test_emit_and_read_back(self):
        d = tempfile.mkdtemp()
        cfg, _ = config.load(ROOT)
        m = metrics.Metrics(d, cfg)
        m.emit("attempt", outcome="ok", skill="s", elapsed_ms=5)
        evs = m.events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["outcome"], "ok")

    def test_emit_never_raises_on_bad_path(self):
        cfg, _ = config.load(ROOT)
        m = metrics.Metrics("/nonexistent/\x00bad", cfg)
        m.emit("attempt", outcome="ok")   # must not raise

    def test_latest_run(self):
        d = tempfile.mkdtemp()
        cfg, _ = config.load(ROOT)
        m = metrics.Metrics(d, cfg)
        m.emit("attempt", outcome="ok")
        m2 = metrics.Metrics(d, cfg)
        self.assertEqual(m2.run, m.run)


if __name__ == "__main__":
    unittest.main()
