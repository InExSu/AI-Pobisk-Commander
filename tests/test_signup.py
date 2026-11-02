"""The signup loop, driven without a browser and without a model.

Two claims are under test, and both are about *not* needing anything:

* the ordinary form is finished by rules alone — no API call, no token spent;
* the interesting parts (code from mail, loop guard, captcha) are decided by
  code, not by the model's mood.

The fake driver keeps values in the HTML it returns, so "fill then look again"
behaves like a browser: without that, the heuristic would see the same empty
field forever and the loop guard would fire on every test.
"""
import copy
import os
import re
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import config      # noqa: E402
import signup as S  # noqa: E402

FORM = """
<form>
  <label for="m">Почта</label>
  <input data-pb-ref="f1" id="m" type="email" name="email" required>
  <input data-pb-ref="f2" type="password" name="password" required>
  <button data-pb-ref="f3" type="submit">Зарегистрироваться</button>
</form>
"""

CODE_PAGE = """
<h1>Введите код из письма</h1>
<form>
  <label for="c">Код из письма</label>
  <input data-pb-ref="f4" id="c" name="code">
  <button data-pb-ref="f5">Подтвердить</button>
</form>
"""

DONE_PAGE = "<h1>Спасибо за регистрацию!</h1><p>Аккаунт создан.</p>"

# A captcha the human must pass: the widget is on screen, so the driver marks
# the iframe. A script alone is not enough — see the invisible variant below.
CAPTCHA_PAGE = """
<h1>Подтвердите, что вы не робот</h1>
<iframe data-pb-captcha="visible" src="https://hcaptcha.com/captcha/v1/x"></iframe>
<form>
  <input data-pb-ref="f1" type="email" name="email" value="a@b.ru">
  <input data-pb-ref="f2" type="password" name="password" value="x">
</form>
"""

INVISIBLE_CAPTCHA_PAGE = """
<h1>Регистрация</h1>
<script src="https://www.google.com/recaptcha/api.js"></script>
<form>
  <input data-pb-ref="f1" type="email" name="email" value="a@b.ru">
  <input data-pb-ref="f2" type="password" name="password" value="x">
  <button data-pb-ref="f3">Создать аккаунт</button>
</form>
"""


class FakeDriver:
    def __init__(self, pages, advance_on_click=True,
                 url="https://example.com/register"):
        self.pages = pages
        self.i = 0
        self.values = {}
        self.checked = set()
        self.actions = []
        self.advance = advance_on_click
        self._url = url

    def goto(self, url):
        self._url = url
        self.actions.append(("goto", url))

    def url(self):
        return self._url

    def html(self):
        html = self.pages[min(self.i, len(self.pages) - 1)]
        for ref, val in self.values.items():
            html = re.sub(r'(<input[^>]*data-pb-ref="%s"[^>]*?)>' % re.escape(ref),
                          lambda m: '%s value="%s">' % (m.group(1), val), html)
        for ref in self.checked:
            html = re.sub(r'(<input[^>]*data-pb-ref="%s"[^>]*?)>' % re.escape(ref),
                          lambda m: "%s checked>" % m.group(1), html)
        return html

    def fill(self, ref, value):
        self.actions.append(("fill", ref, value))
        self.values[ref] = value

    def click(self, ref):
        self.actions.append(("click", ref))
        if self.advance:
            self.i = min(self.i + 1, len(self.pages) - 1)

    def check(self, ref, on=True):
        self.actions.append(("check", ref, on))
        self.checked.add(ref) if on else self.checked.discard(ref)

    def select(self, ref, value):
        self.actions.append(("select", ref, value))

    def press(self, ref, key="Enter"):
        self.actions.append(("press", ref, key))

    def wait(self, ms=0):
        self.actions.append(("wait", ms))

    def screenshot(self, path):
        return ""

    def close(self):
        pass


class ScriptedThinker:
    """Returns canned decisions and records the prompt it was given."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.prompts = []
        self.model = ("fake", "fake")
        self.calls = 0

    def decide(self, view, history, profile, ctx):
        self.prompts.append(view.to_prompt())
        self.calls += 1
        if not self.actions:
            return {"action": "done", "target": "", "value": "",
                    "reason": "no more scripted actions", "confidence": 1.0}
        return dict(self.actions.pop(0), confidence=0.9)


class FakeMail:
    def __init__(self, code="424242", uid="7"):
        self.code = code
        self.uid = uid
        self.waits = 0

    def last_uid(self):
        return 5

    def wait_for_code(self, since_uid=0, timeout=None, hints=()):
        self.waits += 1
        return types.SimpleNamespace(uid=self.uid, subject="Код подтверждения"), self.code


def cfg():
    c = copy.deepcopy(config.DEFAULTS)
    c["state"]["dir"] = ".ai_pobisk_test"
    c["signup"].update({"max_steps": 12, "mail_timeout_sec": 0.1,
                        "max_wall_clock_sec": 30, "max_llm_calls": 3})
    c["meta"]["candidates"] = []
    return c


def profile(email="gh1@igrasu.ru", password="S3cret!pass"):
    return S.Profile({"email": email, "password": password, "name": "Михаил"})


class TestRulesOnly(unittest.TestCase):
    def run_signup(self, pages, thinker=None, mail=None, **kw):
        root = tempfile.mkdtemp()
        driver = FakeDriver(pages)
        runner = S.Signup(root, cfg(), driver, thinker=thinker,
                          mail=mail, profile=profile(), log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True, **kw)
        return res, driver

    def test_ordinary_form_needs_no_model(self):
        thinker = S.Thinker(tempfile.mkdtemp(), cfg(), adapters=None,
                            log=lambda *_: None)
        res, driver = self.run_signup([FORM, DONE_PAGE], thinker=thinker)
        self.assertEqual(res["status"], "registered")
        self.assertEqual(thinker.calls, 0)
        self.assertIn(("fill", "f1", "gh1@igrasu.ru"), driver.actions)
        self.assertTrue(any(a[0] == "click" and a[1] == "f3"
                            for a in driver.actions))

    def test_password_is_generated_and_never_reused_from_the_page(self):
        root = tempfile.mkdtemp()
        driver = FakeDriver([FORM, DONE_PAGE])
        runner = S.Signup(root, cfg(), driver, profile=profile(password=""),
                          log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        filled = [a for a in driver.actions if a[0] == "fill" and a[1] == "f2"]
        self.assertEqual(len(filled), 1)
        self.assertGreaterEqual(len(filled[0][2]), 12)

    def test_dry_run_touches_nothing(self):
        res, driver = self.run_signup([FORM, DONE_PAGE], dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual([a[0] for a in driver.actions], ["goto"])

    def test_trace_file_is_written(self):
        root = tempfile.mkdtemp()
        driver = FakeDriver([FORM, DONE_PAGE])
        runner = S.Signup(root, cfg(), driver, profile=profile(),
                          log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "registered")
        self.assertTrue(os.path.exists(res["trace"]))


class TestCodeFromMail(unittest.TestCase):
    def test_wait_email_then_fill_the_code(self):
        root = tempfile.mkdtemp()
        driver = FakeDriver([FORM, CODE_PAGE, DONE_PAGE])
        mail = FakeMail("424242")
        thinker = ScriptedThinker([
            {"action": "wait_email", "target": "", "value": "", "reason": "code"},
            {"action": "fill", "target": "f4", "value": "{{code}}", "reason": "code"},
        ])
        runner = S.Signup(root, cfg(), driver, thinker=thinker, mail=mail,
                          profile=profile(), log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "registered")
        self.assertEqual(driver.values.get("f4"), "424242")
        self.assertEqual(mail.waits, 1)

    def test_no_letter_means_blocked_not_a_guess(self):
        root = tempfile.mkdtemp()
        driver = FakeDriver([FORM, CODE_PAGE, DONE_PAGE])
        mail = FakeMail(None)
        mail.wait_for_code = lambda **kw: None
        thinker = ScriptedThinker([
            {"action": "wait_email", "target": "", "value": "", "reason": "code"},
        ])
        runner = S.Signup(root, cfg(), driver, thinker=thinker, mail=mail,
                          profile=profile(), log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "blocked")
        self.assertIn("code", res["reason"])

    def test_unresolved_placeholder_is_a_stop(self):
        root = tempfile.mkdtemp()
        driver = FakeDriver([FORM, CODE_PAGE])
        thinker = ScriptedThinker([
            {"action": "fill", "target": "f4", "value": "{{code}}", "reason": "x"},
        ])
        runner = S.Signup(root, cfg(), driver, thinker=thinker,
                          profile=profile(), log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "blocked")
        self.assertIn("placeholder", res["reason"])


class TestGuards(unittest.TestCase):
    def test_same_action_three_times_is_a_loop(self):
        root = tempfile.mkdtemp()
        driver = FakeDriver([FORM], advance_on_click=False)
        thinker = ScriptedThinker([
            {"action": "click", "target": "f3", "value": "", "reason": "again"},
            {"action": "click", "target": "f3", "value": "", "reason": "again"},
            {"action": "click", "target": "f3", "value": "", "reason": "again"},
        ])
        runner = S.Signup(root, cfg(), driver, thinker=thinker,
                          profile=profile(), log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "loop_guard")

    def test_captcha_with_nothing_left_to_fill_blocks(self):
        root = tempfile.mkdtemp()
        driver = FakeDriver([CAPTCHA_PAGE])
        runner = S.Signup(root, cfg(), driver, profile=profile(),
                          log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "blocked")
        self.assertIn("captcha", res["reason"])

    def test_invisible_captcha_does_not_block(self):
        # reCAPTCHA v3 mints its token itself; refusing to submit because the
        # script is loaded would refuse most of the modern web.
        root = tempfile.mkdtemp()
        driver = FakeDriver([INVISIBLE_CAPTCHA_PAGE, DONE_PAGE])
        runner = S.Signup(root, cfg(), driver, profile=profile(),
                          log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "registered")
        self.assertTrue(any(a[0] == "click" and a[1] == "f3"
                            for a in driver.actions))

    def test_model_cannot_name_a_control_that_is_not_there(self):
        root = tempfile.mkdtemp()
        driver = FakeDriver([FORM])
        thinker = S.Thinker(root, cfg(), adapters=None, log=lambda *_: None)
        thinker.decide = lambda *a, **k: {"action": "blocked", "target": "f99",
                                          "value": "", "reason": "no such ref",
                                          "confidence": 0.9}
        runner = S.Signup(root, cfg(), driver, thinker=thinker,
                          profile=profile(), log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "blocked")

    def test_wall_clock_budget(self):
        root = tempfile.mkdtemp()
        c = cfg()
        c["signup"]["max_wall_clock_sec"] = 0
        driver = FakeDriver([FORM], advance_on_click=False)
        runner = S.Signup(root, c, driver, profile=profile(),
                          log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "budget")


class TestHeuristic(unittest.TestCase):
    def view(self, html):
        import pageview
        return pageview.parse(html)

    def test_fills_email_before_password(self):
        act = S.heuristic(self.view(FORM), profile(), {})
        self.assertEqual((act["action"], act["target"]), ("fill", "f1"))
        self.assertEqual(act["value"], "gh1@igrasu.ru")

    def test_unknown_field_asks_the_model(self):
        html = '<input data-pb-ref="f1" name="inn">'
        self.assertIsNone(S.heuristic(self.view(html), profile(), {}))

    def test_optional_unknown_field_is_skipped(self):
        # xkiro.com has a "referral code" field nobody must invent a value for;
        # stalling on it produced a wrong click instead of a submit.
        html = ('<input data-pb-ref="f1" type="email" name="email" value="a@b.ru">'
                '<input data-pb-ref="f2" name="referralCode">'
                '<button data-pb-ref="f3">Получить бонус</button>')
        act = S.heuristic(self.view(html), profile(), {})
        self.assertEqual((act["action"], act["target"]), ("click", "f3"))

    def test_required_unknown_field_asks_the_model(self):
        html = '<input data-pb-ref="f1" name="inn" required>'
        self.assertIsNone(S.heuristic(self.view(html), profile(), {}))

    def test_presses_the_button_that_means_create(self):
        html = ('<input data-pb-ref="f1" type="email" name="email" value="a@b.ru">'
                '<button data-pb-ref="f2">Отмена</button>'
                '<button data-pb-ref="f3">Создать аккаунт</button>')
        act = S.heuristic(self.view(html), profile(), {})
        self.assertEqual((act["action"], act["target"]), ("click", "f3"))

    def test_tab_labelled_register_is_not_the_submit(self):
        # The xkiro.com case: a tab "Регистрация" sits above the form, and the
        # real submit is inside it under a label that matches no keyword.
        html = ('<div><button data-pb-ref="f1">Регистрация</button></div>'
                '<form>'
                '<input data-pb-ref="f2" type="email" name="email" value="a@b.ru">'
                '<input data-pb-ref="f3" type="password" name="password" value="x">'
                '<button data-pb-ref="f4" type="submit">Получить бонус</button>'
                '</form>')
        act = S.heuristic(self.view(html), profile(), {})
        self.assertEqual((act["action"], act["target"]), ("click", "f4"))

    def test_does_not_submit_twice(self):
        html = ('<input data-pb-ref="f1" type="email" name="email" value="a@b.ru">'
                '<button data-pb-ref="f2">Создать аккаунт</button>')
        self.assertIsNone(S.heuristic(self.view(html), profile(),
                                      {"submitted": True}))


class TestPageErrors(unittest.TestCase):
    def test_validation_message_reaches_the_result(self):
        # xkiro.com refuses anything but @gmail/@icloud; the reason the agent
        # reports must be the page's sentence, not the model's guess.
        page = ('<form><input data-pb-ref="f1" name="email" value="a@b.ru">'
                '<p class="text-red-500">Принимаются только адреса @gmail.com</p>'
                '<button data-pb-ref="f2">Создать</button></form>')
        root = tempfile.mkdtemp()
        thinker = ScriptedThinker([
            {"action": "blocked", "target": "", "value": "",
             "reason": "no password provided"},
        ])
        runner = S.Signup(root, cfg(), FakeDriver([page]), thinker=thinker,
                          profile=profile(), log=lambda *_: None)
        res = runner.run("https://example.com/register", allow_live=True)
        self.assertEqual(res["status"], "blocked")
        self.assertIn("@gmail.com", res["reason"])


class TestProfileAndPassword(unittest.TestCase):
    def test_password_is_not_in_the_prompt(self):
        thinker = S.Thinker(tempfile.mkdtemp(), cfg(), adapters=None,
                            log=lambda *_: None)
        prompt = thinker._prompt(self._view(), [], profile())
        self.assertNotIn("S3cret!pass", prompt)
        self.assertIn("***", prompt)

    def _view(self):
        import pageview
        return pageview.parse(FORM)

    def test_generated_password_has_every_class(self):
        pwd = S._gen_password(16)
        self.assertEqual(len(pwd), 16)
        self.assertTrue(any(c.isupper() for c in pwd))
        self.assertTrue(any(c.islower() for c in pwd))
        self.assertTrue(any(c.isdigit() for c in pwd))
        self.assertTrue(any(not c.isalnum() for c in pwd))

    def test_site_overrides_win(self):
        p = S.Profile({"email": "a@b.ru",
                       "sites": {"example.com": {"email": "x@y.ru"}}})
        self.assertEqual(p.for_site("example.com").value("email"), "x@y.ru")
        self.assertEqual(p.for_site("other.ru").value("email"), "a@b.ru")

    def test_json_is_found_inside_prose(self):
        raw = 'Sure! {"action":"click","target":"f1"} hope that helps'
        self.assertEqual(S._json_from(raw)["target"], "f1")

    def test_garbage_json_is_none(self):
        self.assertIsNone(S._json_from("no json here"))


if __name__ == "__main__":
    unittest.main()
