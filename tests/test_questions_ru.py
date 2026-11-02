"""A worker's question must actually reach the competence check.

One live run (2026-11-01, task «03 Иссуес.md») died politely: the model read
the project rules, listed open issues via gh, then asked IN RUSSIAN «какой
именно issue нужно проработать?» — and classify scored the whole thing `ok`,
because QUESTION_PATTERNS only knew English interrogatives and the 600-char
cap ate everything a real CLI transcript carries (tool logs, banners).
The run ended needs_review, stalling a task that the project's own AGENTS.md
 («работай до пустой очереди») had already answered: pick the top issue.

Two holes, two tests: question detection, and what reaches competence.py —
it received outcome.detail ("done", the first line), never the question.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "adapters"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import classify as classify_mod     # noqa: E402
import competence                   # noqa: E402
import journal as journal_mod       # noqa: E402
import meta as meta_mod             # noqa: E402

CFGQ = {"questions": {"auto_answer_from": [], "ask_owner_when_confidence_below": 0.6,
                      "max_pending": 3, "owner_timeout_sec": 600},
        "meta": {"candidates": [], "timeout_sec": 5, "max_reparse": 0},
        "state": {"dir": ".ai_pobisk_test_q", "journal": "j.md",
                  "state_file": "s.json"}}

REAL_QUESTION = ("done\n"
                 "Прочитал AGENTS.md, получил список открытых issue через gh.\n"
                 "Уточните, пожалуйста, какой именно issue нужно проработать?\n"
                 "Например: #782 drakonas не играется (критично), #775, #369.\n"
                 "Или вы имели в виду другую задачу? Укажите номер issue, "
                 "и я приступлю к работе.\n")


class TestClassifyRussianQuestion(unittest.TestCase):
    def test_noisy_transcript_with_russian_question(self):
        # The live shape: tool-log noise (easily >600 chars) before the
        # model's final question. Used to score `ok`.
        noisy = ("[tool] read_files ok\n" * 40) + REAL_QUESTION
        o = classify_mod.classify(noisy, skill="cline")
        self.assertEqual(o.outcome, "question")

    def test_russian_interrogative_without_question_mark(self):
        t = ("Прочитал AGENTS.md. Уточните, какой issue взять в работу — "
             "#782 или #775. Готов приступить.")
        o = classify_mod.classify(t, skill="cline")
        self.assertEqual(o.outcome, "question")

    def test_english_question_still_works(self):
        o = classify_mod.classify("done\nWhich database should I use for this?",
                                  skill="cline")
        self.assertEqual(o.outcome, "question")

    def test_ordinary_report_is_not_a_question(self):
        # Guard against over-detection: a normal completion report with no
        # interrogatives stays ok.
        o = classify_mod.classify("done\nОбновил game.js, тесты зелёные.",
                                  skill="cline")
        self.assertEqual(o.outcome, "ok")


class TestTaskSelectionCompetence(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.j = journal_mod.Journal(self.root, CFGQ)
        self.m = meta_mod.Meta(self.root, CFGQ, {})
        self.m.model = None          # no meta model: rules only

    def test_which_issue_is_answered_without_owner(self):
        # Choosing WHAT to work on is the supervisor's own competence: the
        # standing rule is "work the queue top-down". Used to escalate this
        # to the owner while open issues existed.
        ans = competence.answer_question(
            self.root, CFGQ, self.m,
            "Уточните, какой именно issue нужно проработать? #782 или #775?",
            self.j, "t.md")
        self.assertTrue(ans)
        self.assertIn("issue", ans.lower())

    def test_ordinary_question_still_escalates(self):
        # A question outside the deterministic table keeps the old path:
        # no local sources, no meta model -> owner.
        ans = competence.answer_question(
            self.root, CFGQ, self.m, "Какой пароль от базы данных?", self.j,
            "t.md")
        self.assertIsNone(ans)


if __name__ == "__main__":
    unittest.main()
