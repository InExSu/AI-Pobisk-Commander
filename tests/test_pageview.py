"""The page as the model sees it: six fields and two buttons, not 300 kB.

The contract under test is the one the driver relies on: every control gets a
ref, every ref points at something real, and the ref survives a re-render
because the driver writes it into the DOM. If this drifts, the agent clicks
the wrong thing while every log line still looks plausible.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pageview as pv   # noqa: E402

FORM = """
<html><head><title>Регистрация — Example</title></head><body>
<h1>Создать аккаунт</h1>
<form action="/register" method="post">
  <input type="hidden" name="csrf" value="tok123">
  <label for="mail">Электронная почта</label>
  <input id="mail" type="email" name="email" required>
  <label>Пароль <input type="password" name="password" required></label>
  <input type="text" name="phone" placeholder="Телефон">
  <select name="country"><option value="RU">Россия</option>
    <option value="KZ">Казахстан</option></select>
  <label><input type="checkbox" name="agree"> Принимаю условия</label>
  <button type="submit" class="btn"><span>Создать</span> <b>аккаунт</b></button>
  <a href="/login">Уже есть аккаунт? Войти</a>
</form>
<div class="g-recaptcha" data-sitekey="x"></div>
</body></html>
"""


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.view = pv.parse(FORM, url="https://example.com/register")

    def ref(self, name):
        for n in self.view.nodes:
            if n.name == name:
                return n
        self.fail("no control named %r" % name)

    def test_title_and_url(self):
        self.assertEqual(self.view.title, "Регистрация — Example")
        self.assertEqual(self.view.url, "https://example.com/register")

    def test_hidden_inputs_are_not_controls(self):
        self.assertIsNone(self.view.by_ref("f-csrf"))
        self.assertEqual([n.name for n in self.view.nodes].count("csrf"), 0)

    def test_label_for_is_resolved(self):
        self.assertEqual(self.ref("email").label, "Электронная почта")

    def test_wrapping_label_is_resolved(self):
        self.assertEqual(self.ref("password").label, "Пароль")

    def test_placeholder_is_a_fallback_label(self):
        self.assertEqual(self.ref("phone").label, "Телефон")

    def test_required_is_carried(self):
        self.assertTrue(self.ref("email").required)
        self.assertFalse(self.ref("phone").required)

    def test_button_text_includes_nested_tags(self):
        buttons = self.view.buttons
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].text, "Создать аккаунт")

    def test_select_options(self):
        country = self.ref("country")
        self.assertEqual(country.options, ["RU", "KZ"])

    def test_link_is_a_control_with_href(self):
        links = self.view.links
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].href, "/login")

    def test_captcha_is_flagged(self):
        self.assertIn("captcha", self.view.flags)

    def test_refs_are_unique(self):
        refs = [n.ref for n in self.view.nodes]
        self.assertEqual(len(refs), len(set(refs)))


class TestDriverContract(unittest.TestCase):
    def test_existing_data_ref_is_used(self):
        html = ('<input data-pb-ref="f7" name="a">'
                '<input data-pb-ref="f9" name="b">')
        view = pv.parse(html)
        self.assertEqual([n.ref for n in view.nodes], ["f7", "f9"])

    def test_offscreen_is_dropped_but_keeps_its_ref(self):
        html = ('<input data-pb-ref="f1" data-pb-offscreen="1" name="ghost">'
                '<input data-pb-ref="f2" name="real">')
        view = pv.parse(html)
        self.assertEqual([n.name for n in view.nodes], ["real"])
        self.assertEqual(view.nodes[0].ref, "f2")

    def test_prompt_is_compact_and_numbered(self):
        view = pv.parse(FORM, url="https://example.com/register")
        prompt = view.to_prompt(max_chars=400)
        self.assertIn("URL: https://example.com/register", prompt)
        self.assertIn("FLAGS: captcha", prompt)
        self.assertIn("input:email", prompt)
        self.assertIn("req", prompt)
        self.assertLessEqual(len(prompt), 460)

    def test_prompt_marks_truncation(self):
        long_text = "текст " * 2000
        view = pv.parse("<p>%s</p>" % long_text)
        self.assertIn("truncated", view.to_prompt(max_chars=300))

    def test_signature_changes_when_a_value_changes(self):
        a = pv.parse('<input name="x" value="1">')
        b = pv.parse('<input name="x" value="2">')
        self.assertNotEqual(a.signature(), b.signature())

    def test_signature_is_stable_for_the_same_page(self):
        self.assertEqual(pv.parse(FORM).signature(), pv.parse(FORM).signature())

    def test_empty_page_does_not_raise(self):
        view = pv.parse("")
        self.assertEqual(view.nodes, [])
        self.assertEqual(view.flags, [])

    def test_live_select_value_is_read(self):
        # The driver writes the current option into data-pb-value: a <select>
        # has no value attribute, so without this the loop re-selects forever.
        view = pv.parse('<select data-pb-ref="f1" name="c" data-pb-value="KZ">'
                        '<option>RU</option><option>KZ</option></select>')
        self.assertEqual(view.by_ref("f1").value, "KZ")

    def test_password_value_is_masked_in_the_prompt(self):
        # The driver copies live values into attributes so the snapshot is
        # honest; a password must still never reach the model's prompt.
        view = pv.parse('<input data-pb-ref="f1" type="password" '
                        'name="password" value="hunter2">')
        prompt = view.to_prompt()
        self.assertNotIn("hunter2", prompt)
        self.assertIn('value="***"', prompt)
        self.assertTrue(view.by_ref("f1").value)   # still "filled" for the rules

    def test_error_box_text_is_captured(self):
        view = pv.parse('<p class="text-red-500">Только @gmail.com</p>'
                        '<div role="alert">Пароль слишком короткий</div>')
        self.assertIn("Только @gmail.com", view.errors)
        self.assertIn("Пароль слишком короткий", view.errors)
        self.assertIn("ERRORS:", view.to_prompt())

    def test_success_flag(self):
        view = pv.parse("<p>Спасибо за регистрацию! Проверьте почту.</p>")
        self.assertTrue(pv.is_success(view))


if __name__ == "__main__":
    unittest.main()
