"""Reading the mailbox: which number in a letter is actually the code.

The failure this guards against is not "cannot connect" — that is loud. It is
the quiet one: an agent that types a year, a price or an order id into a
"confirmation code" field, burns the attempt, and reports success to nobody.
So every rule here has a test that says what must NOT be picked.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, ".agents", "skills", "_shared"))

import mailbox as mb   # noqa: E402


class TestCodeExtraction(unittest.TestCase):
    def test_six_digit_code_with_keyword(self):
        text = "Здравствуйте! Ваш код подтверждения: 482913. Он действует 10 минут."
        self.assertEqual(mb.best_code(text), "482913")

    def test_four_digit_code_is_still_a_code(self):
        text = "Код для входа: 5521"
        self.assertEqual(mb.best_code(text), "5521")

    def test_year_is_not_a_code(self):
        text = "© 2024 Example LLC. Код подтверждения: 739201"
        self.assertEqual(mb.best_code(text), "739201")

    def test_year_alone_is_not_a_code(self):
        text = "Спасибо за покупку в 2024 году. Чек № 2024-000123."
        self.assertIsNone(mb.best_code(text))

    def test_phone_is_not_a_code(self):
        text = "Телефон поддержки: +7 (999) 123-45-67"
        self.assertIsNone(mb.best_code(text))

    def test_price_is_not_a_code(self):
        text = "Итого к оплате: 1290 руб. Код заказа 4401"
        # "код" is near 4401 as well — the keyword wins over the price penalty
        # only for the number the keyword actually belongs to.
        self.assertEqual(mb.best_code(text), "4401")

    def test_no_number_means_no_guess(self):
        self.assertIsNone(mb.best_code("Подтвердите адрес по ссылке ниже."))
        self.assertEqual(mb.extract_codes(""), [])

    def test_ranking_prefers_keyword_neighbour(self):
        text = ("Заказ 987654 создан. "
                "Ваш код: 123456")
        ranked = mb.extract_codes(text)
        self.assertEqual(ranked[0], "123456")


class TestTextAndLinks(unittest.TestCase):
    def test_strip_html(self):
        html = "<html><body><p>Код: <b>123456</b></p><script>x()</script></body></html>"
        text = mb.strip_html(html)
        self.assertIn("Код: 123456", text)
        self.assertNotIn("x()", text)

    def test_links_are_filtered_to_the_site(self):
        html = ('<a href="https://evil.example/x">x</a>'
                '<a href="https://igrasu.ru/confirm?token=abc">confirm</a>'
                '<a href="https://mail.igrasu.ru/track">t</a>')
        links = mb.extract_links(html, allow_hosts=["igrasu.ru"])
        self.assertEqual(len(links), 2)
        self.assertTrue(links[0].startswith("https://igrasu.ru/confirm"))

    def test_plain_url_in_text_is_found(self):
        text = "Подтвердите: https://site.ru/verify/abc123 ."
        links = mb.extract_links(text, allow_hosts=["site.ru"])
        self.assertEqual(links, ["https://site.ru/verify/abc123"])

    def test_entities_are_decoded(self):
        html = '<a href="https://a.ru/confirm?x=1&amp;y=2">go</a>'
        self.assertIn("&y=2", mb.extract_links(html)[0])


def _raw(subject="Код", body="Ваш код: 445566", charset="utf-8"):
    return (("From: Shop <no-reply@shop.ru>\r\n"
             "Subject: %s\r\n"
             "Date: Sat, 31 Oct 2026 12:00:00 +0300\r\n"
             "MIME-Version: 1.0\r\n"
             "Content-Type: text/plain; charset=%s\r\n"
             "Content-Transfer-Encoding: 8bit\r\n\r\n%s")
            % (subject, charset, body)).encode(charset)


class StubIMAP:
    """Just enough IMAP: search returns all uids, fetch returns one body."""

    def __init__(self, raws):
        self.raws = raws
        self.searches = []

    def uid(self, cmd, *args):
        if cmd == "search":
            self.searches.append(args[1:])
            ids = b" ".join(str(i + 1).encode() for i in range(len(self.raws)))
            return ("OK", [ids])
        if cmd == "fetch":
            idx = int(args[0]) - 1
            return ("OK", [(b"1 (BODY[] {%d}" % len(self.raws[idx]),
                            self.raws[idx])])
        return ("NO", [None])

    def select(self, *a, **kw):
        return ("OK", [b"1"])

    def close(self):
        pass

    def logout(self):
        pass


CONF = {"host": "mail.igrasu.ru", "port": 993, "ssl": True,
        "user": "gh1@igrasu.ru", "folder": "INBOX", "poll_sec": 0.01,
        "timeout_sec": 1, "code_regex": ""}


class TestMailClient(unittest.TestCase):
    def mail(self, raws):
        return mb.Mail(CONF, password="secret", imap=StubIMAP(raws))

    def test_fetch_decodes_cyrillic(self):
        m = self.mail([_raw(body="Ваш код: 778899", charset="cp1251")])
        msg = m.fetch(1)
        self.assertIn("Ваш код", msg.body)
        self.assertEqual(msg.code(), "778899")

    def test_wait_for_code_skips_letters_without_a_code(self):
        m = self.mail([_raw(body="Спасибо за подписку"),
                       _raw(body="Код подтверждения: 331122")])
        got = m.wait_for_code(since_uid=0, timeout=0.5)
        self.assertIsNotNone(got)
        msg, code = got
        self.assertEqual(code, "331122")
        self.assertEqual(msg.uid, "2")

    def test_wait_returns_none_on_timeout(self):
        m = self.mail([_raw(body="Ничего полезного")])
        self.assertIsNone(m.wait_for_code(since_uid=0, timeout=0.05))

    def test_watermark_is_sent_to_the_server(self):
        stub = StubIMAP([_raw()])
        m = mb.Mail(CONF, password="x", imap=stub)
        m.uids(since_uid=41)
        crit = stub.searches[-1]
        self.assertIn("UID", crit)
        self.assertIn("42:*", crit)

    def test_non_ascii_subject_asks_for_utf8(self):
        stub = StubIMAP([_raw()])
        m = mb.Mail(CONF, password="x", imap=stub)
        m.uids(subject="подтверждение")
        self.assertEqual(stub.searches[-1][0], "CHARSET")

    def test_code_regex_escape_hatch(self):
        conf = dict(CONF, code_regex=r"код\s*[:—-]\s*([A-Z0-9]{6})")
        m = mb.Mail(conf, password="x",
                    imap=StubIMAP([_raw(body="Ваш код: AB12CD")]))
        self.assertEqual(m.wait_for_code(since_uid=0, timeout=0.2)[1], "AB12CD")

    def test_missing_password_is_a_clear_error(self):
        m = mb.Mail(dict(CONF, password_secret=""), password="")
        with self.assertRaises(mb.MailError) as ctx:
            m.connect()
        self.assertIn("password", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
