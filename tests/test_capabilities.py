"""Capability routing: a step that edits files never reaches a worker that
cannot edit files.

nvidia and tokenharbor are bare chat completions — they post the prompt to an
HTTP endpoint and print the answer. There is no agent behind them, so they
cannot read the repo, only talk about it. Handed a file-editing step they
answer "I don't have access to your filesystem" and exit 0, which reads as
success while nothing happened.

So the supervisor drops them from the worker list of a file step. These tests
pin that behaviour: what counts as a file step, what overrides it, and the
guarantee that a task is never stranded.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import classify                                  # noqa: E402
import dispatcher                                # noqa: E402
import journal as J                              # noqa: E402
import meta as M                                 # noqa: E402
import supervisor as S                           # noqa: E402

# Capture the genuine defaults once, before any test mutates `classify`'s
# process-global no-filesystem config. `Supervisor.__init__` calls
# `set_no_filesystem` from the config it is given, so every `eligible()` call
# below leaves the global set to whatever that config named — `TestFileStep-
# Detection`/`TestRoutingGuards` build Supervisors with no_filesystem=api. The
# per-class tearDown must restore the DEFAULTS, not the pre-test (already
# polluted) value, or a later file (`test_classify`) sees the wrong skill set.
_NO_FS_DEFAULT_SKILLS = set(classify._NO_FS_SKILLS)
_NO_FS_DEFAULT_PATTERNS = classify._NO_FS_PATTERNS

CFG = {
    "meta": {"candidates": [], "timeout_sec": 30, "max_reparse": 0},
    "budget": {"max_retries_per_step": 3, "max_model_rotations": 10,
               "max_account_rotations": 5, "max_meta_calls_per_task": 40,
               "max_wall_clock_sec": 3600, "max_steps_per_task": 50,
               "step_timeout_sec": 60},
    "fallback": {"on_meta_unavailable": "rules_only",
                 "fatal_confidence_floor": 0.7},
    "workers": {"order": ["agent", "api"], "non_workers": [],
                "no_filesystem": {"skills": ["api"]}},
    "questions": {"auto_answer_from": [], "ask_owner_when_confidence_below": 0.6,
                  "max_pending": 3, "owner_timeout_sec": 600},
    "tasks": {"dir": "configs/tasks", "skip_empty": True},
    "state": {"dir": ".ai_pobisk_test_cap", "journal": "j.md",
              "state_file": "s.json"},
}


class Fake:
    """A worker that only records what it was asked to do."""

    def __init__(self, skill, has_filesystem=True):
        self.skill = skill
        self.script_path = "/bin/true"
        self.has_filesystem = has_filesystem
        self.seen = []

    def available(self):
        return True

    def models(self):
        return ["m1"]

    def run(self, prompt, model=None, account=None, timeout=None):
        self.seen.append(prompt)
        from outcome import Outcome
        return Outcome("ok", self.skill, model or "m1", "acct", "sim")


def make_supervisor(adapters):
    """`adapters` is a list of Fake; the supervisor wants skill -> adapter."""
    by_skill = {a.skill: a for a in adapters}
    j = J.Journal(ROOT, CFG)
    j.reset()
    m = M.Meta(ROOT, CFG, by_skill)
    m.model = None
    sv = S.Supervisor(ROOT, CFG, by_skill, m, j, verbose=False)
    sv.worker_order = [a.skill for a in adapters]
    return sv, j


def eligible(body, step="сделай работу", adapters=None):
    """The worker list a step with this body would be dispatched from."""
    if adapters is None:
        adapters = [Fake("agent", True), Fake("api", False)]
    sv, j = make_supervisor(adapters)
    task = dispatcher.Task("/tmp/t.md", "t", body, 1)
    out = sv._eligible_workers(task, step, 1)
    j.reset()
    return out


class TestAdapterCapability(unittest.TestCase):
    def test_api_skills_declare_no_filesystem(self):
        from adapters.cline import ClineAdapter
        from adapters.nvidia import NvidiaAdapter
        from adapters.opencode import OpenCodeAdapter
        from adapters.tokenharbor import TokenHarborAdapter
        self.assertFalse(NvidiaAdapter.has_filesystem)
        self.assertFalse(TokenHarborAdapter.has_filesystem)
        self.assertTrue(ClineAdapter.has_filesystem)
        self.assertTrue(OpenCodeAdapter.has_filesystem)

    def test_base_adapter_assumes_filesystem(self):
        from adapters.base import Adapter
        # Default True: a new adapter is a local agent until it says otherwise.
        self.assertTrue(Adapter.has_filesystem)


class TestFileStepDetection(unittest.TestCase):
    def test_task_naming_a_directory_is_a_file_step(self):
        # The repo root exists, so this body resolves to a real directory.
        self.assertEqual(eligible("Прочитать %s/AGENTS.md." % ROOT),
                         ["agent"])

    def test_plain_text_task_keeps_every_worker(self):
        self.assertEqual(eligible("Напиши мне смешную историю про кота"),
                         ["agent", "api"])

    def test_file_words_make_it_a_file_step(self):
        self.assertEqual(eligible("Поправь конфиг, задеплой в репозиторий"),
                         ["agent"])

    def test_explicit_text_directive_wins_over_the_path(self):
        body = "# requires: text\nПерескажи %s/AGENTS.md своими словами." % ROOT
        self.assertEqual(eligible(body), ["agent", "api"])

    def test_explicit_filesystem_directive_wins_over_plain_words(self):
        body = "# requires: filesystem\nПридумай название для игры."
        self.assertEqual(eligible(body), ["agent"])

    def test_code_and_translate_are_not_file_words(self):
        # "code" and "translate" appear in plenty of pure text tasks that a
        # completion API handles perfectly well. Keeping them out of the list
        # is what stops the router from throwing away a cheap, capable worker.
        self.assertEqual(eligible("Переведи этот код на Python и объясни его"),
                         ["agent", "api"])


class TestRoutingGuards(unittest.TestCase):
    def test_task_is_never_stranded(self):
        # Every worker says it has no filesystem: the step still gets a worker
        # list, because an empty list would fail the task without trying.
        adapters = [Fake("api1", False), Fake("api2", False)]
        out = eligible("Правь файлы в репозитории", adapters=adapters)
        self.assertEqual(out, ["api1", "api2"])

    def test_config_can_exclude_an_extra_worker(self):
        cfg = dict(CFG)
        cfg["workers"] = dict(CFG["workers"])
        cfg["workers"]["no_filesystem"] = {"skills": ["api", "agent"]}
        adapters = [Fake("agent", True), Fake("api", False)]
        by_skill = {a.skill: a for a in adapters}
        j = J.Journal(ROOT, cfg)
        j.reset()
        m = M.Meta(ROOT, cfg, by_skill)
        m.model = None
        sv = S.Supervisor(ROOT, cfg, by_skill, m, j, verbose=False)
        sv.worker_order = ["agent", "api"]
        task = dispatcher.Task("/tmp/t.md", "t", "Почини репозиторий", 1)
        out = sv._eligible_workers(task, "Почини репозиторий", 1)
        j.reset()
        # The config excluded both, and the adapter attribute agrees, so the
        # fallback keeps the task alive instead of failing it outright.
        self.assertEqual(out, ["agent", "api"])

    def test_a_text_step_never_loses_workers(self):
        adapters = [Fake("agent", True), Fake("api", False)]
        out = eligible("Сгенерируй список идей", adapters=adapters)
        self.assertEqual(out, ["agent", "api"])


class TestConfiguredPatternsAreLive(unittest.TestCase):
    def setUp(self):
        # Record the value we are about to change so the restore is exact for
        # whatever this single test configured — but the module-level defaults
        # are what protect every test that runs afterwards.
        self._skills = set(classify._NO_FS_SKILLS)
        self._patterns = classify._NO_FS_PATTERNS

    def tearDown(self):
        # Restore the genuine defaults so a later test file (test_classify) is
        # not polluted by the configs exercised above.
        classify._NO_FS_SKILLS = set(_NO_FS_DEFAULT_SKILLS)
        classify._NO_FS_PATTERNS = _NO_FS_DEFAULT_PATTERNS


def tearDownModule():
    # unittest orders test CLASSES alphabetically, not by definition order, so
    # TestFileStepDetection / TestRoutingGuards (which call eligible() -> build a
    # Supervisor -> set_no_filesystem from their config) run AFTER
    # TestConfiguredPatternsAreLive and leave classify's process-global
    # no-filesystem config pointing at whatever their CFG named. Reset the
    # genuine defaults once, after every test in this module, so the next file
    # (test_classify) starts clean.
    classify._NO_FS_SKILLS = set(_NO_FS_DEFAULT_SKILLS)
    classify._NO_FS_PATTERNS = _NO_FS_DEFAULT_PATTERNS

    def test_config_patterns_are_applied(self):
        classify.set_no_filesystem(["api"], [r"cannot touch the disk"])
        o = classify.classify("I cannot touch the disk, sorry",
                              skill="api")
        self.assertEqual(o.outcome, "rotate_account")

    def test_skill_list_from_config_is_applied(self):
        classify.set_no_filesystem(["weird_skill"], None)
        o = classify.classify("I don't have access to your filesystem",
                              skill="weird_skill")
        self.assertEqual(o.outcome, "rotate_account")

    def test_a_broken_pattern_does_not_disable_the_check(self):
        classify.set_no_filesystem(["api"], [r"(unclosed"])
        # The bad pattern is ignored, the default patterns still work.
        o = classify.classify("I don't have access to your filesystem",
                              skill="api")
        self.assertEqual(o.outcome, "rotate_account")


if __name__ == "__main__":
    unittest.main()
