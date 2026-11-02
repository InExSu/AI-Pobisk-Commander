"""Who actually served the call, and who writes the health store.

Two defects hid in the same place: the rotate scripts pick the model
themselves and never told the Python side, so every attempt event in the
journal carried `model: ""`; and both the scripts and the adapter wrote the
shared health store, so one real call was counted twice — one real failure
became two strikes on a breaker that opens after three.

These tests pin the fix: the skill's own report is the attribution, and the
store has exactly one writer per call.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "adapters"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import base as base_mod                       # noqa: E402
import cline as cline_mod                    # noqa: E402
import opencode as opencode_mod              # noqa: E402

MS = os.path.join(ROOT, ".agents", "skills", "_shared", "model-stats.py")


def load_ms(tmpdir):
    os.environ["AI_ROTATE_DIR"] = tmpdir
    spec = importlib.util.spec_from_file_location(
        "ms_%d" % time.time_ns(), MS)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestAttributionParsing(unittest.TestCase):
    """The marker is the only unambiguous source: the agent's own answer is
    printed to the same stream and may contain the words "model=" or
    "account="."""

    def test_marker_is_parsed(self):
        text = "[2026-11-01 09:45:46] pobisk-attr: model=llama-3.3-free account=acc1"
        self.assertEqual(base_mod.parse_attribution(text),
                         ("llama-3.3-free", "acc1"))

    def test_agent_text_is_not_attribution(self):
        # The answer itself mentions model= and account= — without the marker
        # this would be read as "the model that served".
        text = ("Done. I set model=hallucinated account=nobody\n"
                "pobisk-attr: model=real-model account=real-account")
        self.assertEqual(base_mod.parse_attribution(text),
                         ("real-model", "real-account"))

    def test_last_marker_wins(self):
        # The scripts rotate internally; the final marker names the model that
        # actually served.
        text = ("pobisk-attr: model=first account=a1\n"
                "pobisk-attr: model=second account=a2\n")
        self.assertEqual(base_mod.parse_attribution(text),
                         ("second", "a2"))

    def test_no_marker_means_no_attribution(self):
        self.assertEqual(base_mod.parse_attribution("just some output\n"),
                         ("", ""))

    def test_empty_input(self):
        self.assertEqual(base_mod.parse_attribution(None), ("", ""))
        self.assertEqual(base_mod.parse_attribution(""), ("", ""))


class TestAdapterCarriesTheModel(unittest.TestCase):
    """The journal's model column was empty because the adapters never
    learned which model answered."""

    def _patch(self, mod, rc, out, err=""):
        orig = mod.run_cmd
        mod.run_cmd = lambda *a, **k: (rc, out, err)
        return orig

    def test_cline_reports_the_model_it_used(self):
        a = cline_mod.ClineAdapter(ROOT)
        orig = self._patch(cline_mod, 0,
                           "pobisk-attr: model=llama-3.3-free account=acc1\n"
                           "Done: 3 files changed")
        try:
            o = a.run("сделай работу")
        finally:
            cline_mod.run_cmd = orig
        self.assertEqual(o.model, "llama-3.3-free")
        self.assertEqual(o.account, "acc1")
        self.assertEqual(o.outcome, "ok")

    def test_cline_reports_the_model_on_failure_too(self):
        a = cline_mod.ClineAdapter(ROOT)
        orig = self._patch(cline_mod, 5,
                           "pobisk-attr: model=llama-3.3-free account=acc1\n"
                           "cline-rotate: all accounts exhausted")
        try:
            o = a.run("сделай работу")
        finally:
            cline_mod.run_cmd = orig
        self.assertEqual(o.model, "llama-3.3-free")
        self.assertEqual(o.outcome, "rotate_account")

    def test_opencode_reports_the_model_it_used(self):
        a = opencode_mod.OpenCodeAdapter(ROOT)
        orig = self._patch(opencode_mod, 0,
                           "pobisk-attr: model=opencode/glm-5.3 account=oc1\n"
                           "done")
        try:
            o = a.run("сделай работу")
        finally:
            opencode_mod.run_cmd = orig
        self.assertEqual(o.model, "opencode/glm-5.3")
        self.assertEqual(o.account, "oc1")

    def test_pinned_model_is_the_fallback(self):
        # No marker in the output: the pinned model is still the best guess.
        a = opencode_mod.OpenCodeAdapter(ROOT)
        orig = self._patch(opencode_mod, 0, "done")
        try:
            o = a.run("сделай работу", model="pinned/model")
        finally:
            opencode_mod.run_cmd = orig
        self.assertEqual(o.model, "pinned/model")


class TestSingleWriter(unittest.TestCase):
    """The scripts already write the store. A second writer double-counted:
    one real failure became two strikes against a three-strike breaker."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.environ["AI_ROTATE_DIR"] = self.dir
        self.addCleanup(os.environ.pop, "AI_ROTATE_DIR", None)

    def _store(self):
        p = os.path.join(self.dir, "model-stats.json")
        if not os.path.exists(p):
            return {}
        return json.load(open(p))

    def test_every_script_backed_skill_declares_the_script_as_writer(self):
        for skill in ("cline", "opencode", "nvidia", "tokenharbor", "freebuff"):
            a = base_mod.Adapter(ROOT)
            a.skill = skill
            # The attribute lives on the concrete adapter classes.
            import nvidia as nv, tokenharbor as th, freebuff as fb
            classes = [cline_mod.ClineAdapter, opencode_mod.OpenCodeAdapter,
                       nv.NvidiaAdapter, th.TokenHarborAdapter,
                       fb.FreeBuffAdapter]
            found = [c for c in classes if c.skill == skill]
            self.assertTrue(found, "no adapter for %s" % skill)
            self.assertTrue(found[0].script_records_health,
                            "%s must not write the store twice" % skill)

    def test_adapter_does_not_write_when_the_script_does(self):
        a = opencode_mod.OpenCodeAdapter(ROOT)
        orig = opencode_mod.run_cmd
        opencode_mod.run_cmd = lambda *a_, **k: (
            0, "pobisk-attr: model=m1 account=a1\ndone", "")
        try:
            o = a.run("x")
        finally:
            opencode_mod.run_cmd = orig
        self.assertEqual(o.model, "m1")
        # The adapter did not touch the store — only the script may write it.
        self.assertEqual(self._store(), {})

    def test_adapter_writes_when_nothing_else_does(self):
        # A skill without its own script writer still feeds the store.
        class Solo(base_mod.Adapter):
            skill = "solo"
            script_records_health = False
        a = Solo(ROOT)
        a._invoke = lambda *a_, **k: (0, "done", "")
        o = a.run("x")
        self.assertEqual(o.outcome, "ok")
        # The store has no entry for this model (no marker -> no model name),
        # but the call did not blow up either.
        self.assertEqual(self._store(), {})


class TestAttemptNumber(unittest.TestCase):
    """`attempt_no` was permanently 0: the counter key was never bumped."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        import journal as journal_mod
        self.journal_mod = journal_mod
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        import config
        cfg, _ = config.load(ROOT)
        cfg["state"]["dir"] = self.dir
        self.j = journal_mod.Journal(ROOT, cfg)

    def test_bump_returns_increasing_numbers(self):
        self.assertEqual(self.j.bump("t", "attempt"), 1)
        self.assertEqual(self.j.bump("t", "attempt"), 2)
        self.assertEqual(self.j.bump("t", "attempt"), 3)

    def test_attempts_dict_exposes_the_key(self):
        self.j.bump("t", "attempt")
        self.assertEqual(self.j.attempts("t").get("attempt"), 1)


class TestRepair(unittest.TestCase):
    """A park bans a model for a day, so a park for the wrong reason
    removes a working model from rotation."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.ms = load_ms(self.dir)

    def _entry(self, skill, model):
        return self.ms.load()[skill][model]

    def test_credential_reason_is_recognised(self):
        self.assertTrue(self.ms.is_credential_reason("Invalid API key"))
        self.assertTrue(self.ms.is_credential_reason("HTTP 401 Unauthorized"))
        self.assertTrue(self.ms.is_credential_reason("key has expired"))

    def test_provider_error_is_not_a_credential_reason(self):
        self.assertFalse(self.ms.is_credential_reason(
            "Failed to create stream: inference request failed: failed to "
            "invoke model 'liquid/lfm-2.5'"))
        self.assertFalse(self.ms.is_credential_reason(
            "Insufficient balance. Your Cline Credits balance is $-0.04"))

    def test_false_park_is_released(self):
        self.ms.cmd_record(["cline", "m1", "auth", "0",
                            "Insufficient balance. Your Cline Credits balance "
                            "is $-0.04"])
        self.assertEqual(self._entry("cline", "m1")["state"], "auth_error")
        self.ms.cmd_repair([])
        e = self._entry("cline", "m1")
        self.assertNotEqual(e["state"], "auth_error")
        self.assertEqual(e["cooldown_until"], 0)
        self.assertEqual(e["parks"], 0)

    def test_real_credential_park_is_kept(self):
        self.ms.cmd_record(["cline", "m2", "auth", "0", "Invalid API key"])
        self.ms.cmd_repair([])
        self.assertEqual(self._entry("cline", "m2")["state"], "auth_error")

    def test_released_model_returns_to_rotation(self):
        self.ms.cmd_record(["cline", "m3", "ok", "100", ""])
        self.ms.cmd_record(["cline", "m3", "auth", "0", "Insufficient balance"])
        self.ms.cmd_repair([])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ms.cmd_order(["cline", "--models", "m3"])
        self.assertIn("m3", buf.getvalue())

    def test_park_budget_is_not_spent_by_a_false_park(self):
        # A model parked three times is dropped for good. A false park must
        # not spend that budget.
        for _ in range(3):
            self.ms.cmd_record(["cline", "m4", "auth", "0",
                                "Insufficient balance"])
        self.ms.cmd_repair([])
        self.assertEqual(self._entry("cline", "m4")["parks"], 0)


if __name__ == "__main__":
    unittest.main()
