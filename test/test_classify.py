"""classify.py is the decision table the whole supervisor rests on."""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))
import classify


class TestHttpCodes(unittest.TestCase):
    """Status codes are checked before prose: they lie less."""
    def test_402_is_account_not_model(self):
        o = classify.classify("anything", http=402)
        self.assertEqual(o.outcome, "rotate_account")

    def test_401_403_are_account(self):
        for code in (401, 403):
            self.assertEqual(classify.classify("x", http=code).outcome,
                             "rotate_account")

    def test_404_is_model(self):
        self.assertEqual(classify.classify("x", http=404).outcome, "rotate_model")

    def test_429_5xx_are_retry_never_fatal(self):
        for code in (429, 500, 502, 503, 504, 529):
            self.assertEqual(classify.classify("x", http=code).outcome, "retry")


class TestProsePatterns(unittest.TestCase):
    """Real error strings lifted from the five skills."""
    def test_cline_daily_limit_is_model(self):
        o = classify.classify("error: Daily free model limit reached")
        self.assertEqual(o.outcome, "rotate_model")

    def test_cline_todays_limit(self):
        self.assertEqual(
            classify.classify("You have reached today's free usage limit").outcome,
            "rotate_model")

    def test_nvidia_function_not_found(self):
        self.assertEqual(
            classify.classify("Function 'abc-123' not found").outcome, "rotate_model")

    def test_tokenharbor_empty_wallet(self):
        self.assertEqual(
            classify.classify("Your Token Harbor balance is at $0").outcome,
            "rotate_account")

    def test_credit_insufficient_is_account(self):
        # The trap: looks model-ish, is really the account's daily cap.
        self.assertEqual(
            classify.classify("credit insufficient balance: balance=2021 required=2250").outcome,
            "rotate_account")

    def test_timeout_is_retry(self):
        self.assertEqual(classify.classify("TimeoutError").outcome, "retry")

    def test_overloaded_is_retry(self):
        self.assertEqual(
            classify.classify("Service temporarily overloaded").outcome, "retry")

    def test_invalid_key_is_account(self):
        self.assertEqual(classify.classify("Invalid API key").outcome,
                         "rotate_account")

    def test_no_authToken_is_account(self):
        self.assertEqual(classify.classify("no authToken").outcome,
                         "rotate_account")

    def test_opencode_free_limit(self):
        self.assertEqual(classify.classify("FreeUsageLimitError").outcome,
                         "rotate_model")


class TestBenign(unittest.TestCase):
    def test_plain_answer_is_ok_not_fatal(self):
        # Regression: an unrecognised healthy answer must NOT be fatal.
        self.assertEqual(classify.classify("The answer is 4").outcome, "ok")

    def test_empty_is_retry(self):
        self.assertEqual(classify.classify("").outcome, "retry")

    def test_question_detected(self):
        self.assertEqual(classify.classify("Which database should I use?").outcome,
                         "question")


class TestBrief(unittest.TestCase):
    def test_detail_is_one_line(self):
        o = classify.classify("line one\nline two", http=500)
        self.assertNotIn("\n", o.detail)

    def test_raw_kept(self):
        o = classify.classify("some output here")
        self.assertIn("some output", o.raw)


if __name__ == "__main__":
    unittest.main()


class TestMissingBinary(unittest.TestCase):
    """A missing binary fails in ms and never self-heals: must not be `retry`."""
    def test_no_such_file(self):
        o = classify.classify("/path/opencode: No such file or directory")
        self.assertEqual(o.outcome, "rotate_account")

    def test_command_not_found(self):
        self.assertEqual(classify.classify("qwen: command not found").outcome,
                         "rotate_account")

    def test_permission_denied(self):
        self.assertEqual(classify.classify("bash: ./x: Permission denied").outcome,
                         "rotate_account")

    def test_real_answer_not_flagged(self):
        self.assertEqual(classify.classify("The answer is 4").outcome, "ok")


class TestNoFilesystemCapability(unittest.TestCase):
    """A bare API call saying 'no file access' is a capability gap, not success."""
    def test_russian(self):
        o = classify.classify("Я не имею доступа к файловой системе вашего компьютера",
                              skill="nvidia")
        self.assertEqual(o.outcome, "rotate_account")

    def test_english(self):
        o = classify.classify("the file system available to me does not contain that path",
                              skill="tokenharbor")
        self.assertEqual(o.outcome, "rotate_account")

    def test_agent_skill_not_flagged(self):
        # cline HAS file tools; if it says this it is context, not a gap.
        o = classify.classify("Я не имею доступа к файловой системе", skill="cline")
        self.assertEqual(o.outcome, "ok")
