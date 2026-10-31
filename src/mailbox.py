"""mailbox.py — reading the registration mailbox over IMAP, on the stdlib.

Why not `imapclient`/`mailparser`: the whole project runs on "clone and run".
Python ships `imaplib` and `email`, and the only thing they lack for this job
is *interpretation* — deciding which 6 digits in a letter are the code and
which are a year, a price or a phone number. That part is ours, and it is the
part worth testing.

What the registration loop needs from mail:

    last_uid()                  where we were before pressing "submit"
    wait_for_code(since_uid)    block until a letter arrives, return its code
    wait_for_link(since_uid)    same, but for "confirm your address" links

Two invariants matter more than features:

* **read-only.** The folder is opened `readonly` and bodies are fetched with
  `BODY.PEEK[]`, so the agent never marks the owner's mail as read.
* **never guess silently.** `wait_for_code` returns None on timeout instead of
  picking the least-bad number. A wrong code costs a failed registration and
  a locked account; "no code" costs a retry.

The password is never in the repo: `AI_POBISK_IMAP_PASSWORD`, or the shared
store `~/.ai-rotate/secrets.json` under `[mail].password_secret`.
"""

import email
import email.header
import email.utils
import imaplib
import importlib.util
import os
import re
import sys
import time
from html import unescape

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.join(os.path.dirname(_HERE), ".agents", "skills", "_shared")
for _p in (_HERE, _SHARED):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class MailError(RuntimeError):
    """Anything that makes the mailbox unusable right now (auth, TLS, DNS)."""


# ── text extraction ───────────────────────────────────────────────────────────
# Words that, near a number, make it a code. Kept deliberately short: every
# extra word is another chance to promote a price or an order id to "the code".
CODE_HINTS = (
    "код", "code", "подтвержд", "verif", "otp", "pin", "пароль", "password",
    "одноразов", "вход", "login", "activate", "активац",
)

# A code is 4-8 digits, not glued to other digits. `+7 999 123-45-67` must not
# yield "999" — the lookbehind on `+` and the digit guards handle that.
_NUM_RE = re.compile(r"(?<![\d+])(\d{4,8})(?!\d)")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
# A price is a number *attached* to a currency, not a number in the same
# letter: "Итого 1290 руб. Код заказа 4401" must keep 4401.
_PRICE_AFTER = re.compile(r"^\s*(₽|\$|€|руб|rub|usd|eur|р\.)", re.I)
_PRICE_BEFORE = re.compile(r"(цена|price|стоимост\w*|итого|total)\s*[:=]?\s*$", re.I)
_LINK_HINTS = ("confirm", "verify", "activat", "validate", "welcome", "token",
               "signup", "register", "подтверд", "активац", "регистрац")


def _price_adjacent(text, m):
    """Is *this* number a price? Adjacency, not a 100-character window."""
    return bool(_PRICE_AFTER.match(text[m.end():m.end() + 8]) or
                _PRICE_BEFORE.search(text[max(0, m.start() - 24):m.start()]))


def _keyword_score(low, start, end, words):
    """How close the nearest hint word is: adjacent 3, near 2, in the letter 1."""
    best = 0
    for w in words:
        i = low.find(w)
        while i != -1:
            if i >= start and i + len(w) <= end:
                dist = 0
            elif i < start:
                dist = start - (i + len(w))
            else:
                dist = i - end
            if dist <= 12:
                best = max(best, 3)
            elif dist <= 40:
                best = max(best, 2)
            elif dist <= 100:
                best = max(best, 1)
            i = low.find(w, i + 1)
    return best


def strip_html(html):
    """HTML -> readable text. Crude on purpose: mail HTML is not a document."""
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style|head|noscript)\b.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = unescape(s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def ranked_codes(text, hints=()):
    """[(code, score)] best first. Scores are heuristic, not probabilities."""
    if not text:
        return []
    low = text.lower()
    words = tuple(h.lower() for h in hints if h) + CODE_HINTS
    best = {}
    for m in _NUM_RE.finditer(text):
        value = m.group(1)
        before = text[max(0, m.start() - 2):m.start()]
        if before.endswith("+") or before.endswith("("):
            continue                                   # tail of a phone number
        # A keyword is worth more than length: "код 5521" is a code, and
        # "заказ 2024" is not, even though both are four digits. Distance
        # decides which number the keyword belongs to.
        score = _keyword_score(low, m.start(), m.end(), words)
        if len(value) == 4 and value[:2] in ("19", "20"):
            score -= 4                                 # a year, not a code
        if _price_adjacent(text, m):
            score -= 3
        if len(value) in (6, 8):
            score += 1                                 # typical OTP lengths
        best[value] = max(best.get(value, -99), score)
    return sorted(best.items(), key=lambda kv: (-kv[1], -len(kv[0])))


def extract_codes(text, hints=()):
    """Ranked candidate codes, best first. Empty list means "do not guess"."""
    return [v for v, _ in ranked_codes(text, hints)]


def best_code(text, hints=(), min_score=2):
    """The one code worth typing, or None.

    A threshold, not "the top one": a letter that merely *contains* a number
    (an invoice, a newsletter) must not hand the agent something to type.
    """
    for value, score in ranked_codes(text, hints):
        if score >= min_score:
            return value
    return None


def _host(url):
    m = re.match(r"https?://([^/:?#]+)", url or "", re.I)
    return (m.group(1) if m else "").lower()


def extract_links(text, allow_hosts=(), limit=5):
    """Confirmation links, same-site first. `allow_hosts` is a suffix list."""
    if not text:
        return []
    found = []
    for m in _HREF_RE.finditer(text):
        found.append(unescape(m.group(1)).strip())
    for m in _URL_RE.finditer(text):
        found.append(m.group(0).rstrip(".,;)"))
    out, seen = [], set()
    allow = tuple(h.lower().lstrip(".") for h in allow_hosts if h)
    for url in found:
        if not url.lower().startswith("http") or url in seen:
            continue
        host = _host(url)
        if allow and not any(host == a or host.endswith("." + a) for a in allow):
            continue
        seen.add(url)
        low = url.lower()
        rank = 2 if any(h in low for h in _LINK_HINTS) else 0
        if host and any(host.endswith(a) for a in allow):
            rank += 1
        out.append((rank, url))
    out.sort(key=lambda t: -t[0])
    return [u for _, u in out[:limit]]


def _decode_part(part):
    raw = part.get_payload(decode=True)
    if raw is None:
        payload = part.get_payload()
        return payload if isinstance(payload, str) else ""
    charset = (part.get_content_charset() or "").lower()
    for cs in (charset, "utf-8", "cp1251", "koi8-r", "latin-1"):
        if not cs:
            continue
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _header(msg, name):
    raw = msg.get(name) or ""
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        return raw


class Message:
    """One letter, already interpreted: text, codes, links."""

    def __init__(self, uid, subject="", sender="", date=None, text="", html=""):
        self.uid = str(uid)
        self.subject = subject or ""
        self.sender = sender or ""
        self.date = date
        self.text = text or ""
        self.html = html or ""

    @property
    def body(self):
        """Plain text if the letter has it, otherwise the HTML flattened."""
        return self.text.strip() or strip_html(self.html)

    def codes(self, hints=()):
        return extract_codes(self.body + "\n" + self.subject, hints)

    def code(self, hints=()):
        return best_code(self.body + "\n" + self.subject, hints)

    def links(self, allow_hosts=(), limit=5):
        # Links usually live in the HTML part; text-only letters carry them raw.
        return extract_links((self.html or "") + "\n" + self.text, allow_hosts, limit)

    def __repr__(self):
        return "Message(uid=%s, from=%r, subject=%r)" % (
            self.uid, self.sender, self.subject[:60])


# ── the client ────────────────────────────────────────────────────────────────
def _shared_secrets():
    """Load `.agents/skills/_shared/secrets.py` by path.

    By path, not `import secrets`: this module is named `secrets` in the repo
    and would shadow the stdlib one for every other import in the process.
    """
    path = os.path.join(_SHARED, "secrets.py")
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("pobisk_secrets", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def resolve_password(conf):
    """Env var first, shared secret store second. Never the TOML file."""
    env_name = conf.get("password_env") or "AI_POBISK_IMAP_PASSWORD"
    if os.environ.get(env_name):
        return os.environ[env_name]
    name = conf.get("password_secret") or ""
    if not name:
        return ""
    mod = _shared_secrets()
    if not mod:
        return ""
    try:
        entry = (mod.load() or {}).get(name) or {}
    except Exception:
        return ""
    if isinstance(entry, str):
        return entry
    return entry.get("key") or entry.get("value") or ""


class Mail:
    """A read-only IMAP client that answers one question: is there a code yet?"""

    def __init__(self, conf, password=None, folder=None, imap=None):
        self.host = conf.get("host") or ""
        self.port = int(conf.get("port") or 993)
        self.ssl = bool(conf.get("ssl", True))
        self.user = conf.get("user") or ""
        self.password = password if password is not None else resolve_password(conf)
        self.folder = folder or conf.get("folder") or "INBOX"
        self.timeout = float(conf.get("timeout_sec") or 180)
        self.poll = float(conf.get("poll_sec") or 5)
        self.code_regex = conf.get("code_regex") or ""
        self._secret_name = conf.get("password_secret") or ""
        self._imap = imap
        self._connected = imap is not None

    # ── connection ────────────────────────────────────────────────────────────
    def connect(self):
        if self._connected:
            return self
        if not self.host or not self.user:
            raise MailError("[mail] host/user missing in configs/ai_pobisk.toml")
        if not self.password:
            raise MailError(
                "no mailbox password: export AI_POBISK_IMAP_PASSWORD or "
                "python3 .agents/skills/_shared/secrets.py set %s '...'"
                % (self._conf_secret_name() or "imap"))
        try:
            cls = imaplib.IMAP4_SSL if self.ssl else imaplib.IMAP4
            self._imap = cls(self.host, self.port, timeout=30)
            self._imap.login(self.user, self.password)
            # readonly: the agent must not flip the owner's unread flags.
            self._imap.select(self.folder, readonly=True)
            self._connected = True
        except imaplib.IMAP4.error as e:
            raise MailError("IMAP login failed for %s: %s" % (self.user, e))
        except OSError as e:
            raise MailError("cannot reach %s:%s (%s)" % (self.host, self.port, e))
        return self

    def _conf_secret_name(self):
        return getattr(self, "_secret_name", "")

    def close(self):
        try:
            if self._connected and self._imap is not None:
                try:
                    self._imap.close()
                except Exception:
                    pass
                self._imap.logout()
        except Exception:
            pass
        finally:
            self._connected = False

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False

    # ── search ────────────────────────────────────────────────────────────────
    def _criteria(self, unseen=False, days=None, sender=None, subject=None,
                  since_uid=None):
        crit = []
        if unseen:
            crit.append("UNSEEN")
        if days:
            since = time.strftime("%d-%b-%Y", time.localtime(time.time() - days * 86400))
            crit += ["SINCE", since]
        if sender:
            crit += ["FROM", '"%s"' % sender]
        if subject:
            crit += ["SUBJECT", '"%s"' % subject]
        if since_uid:
            crit += ["UID", "%d:*" % (int(since_uid) + 1)]
        # Non-ASCII search terms need CHARSET before them (RFC 3501 6.4.4).
        if any(not c.isascii() for c in crit):
            crit = ["CHARSET", "UTF-8"] + crit
        return crit or ["ALL"]

    def uids(self, unseen=False, days=None, sender=None, subject=None,
             since_uid=None, limit=20):
        self.connect()
        crit = self._criteria(unseen, days, sender, subject, since_uid)
        try:
            typ, data = self._imap.uid("search", None, *crit)
        except Exception as e:
            raise MailError("IMAP search failed: %s" % e)
        if typ != "OK" or not data or not data[0]:
            return []
        ids = [u.decode() if isinstance(u, bytes) else str(u)
               for u in data[0].split() if u]
        return ids[-limit:]

    def last_uid(self):
        """Highest UID present now — the watermark to wait *after*."""
        self.connect()
        try:
            typ, data = self._imap.uid("search", None, "ALL")
            if typ != "OK" or not data or not data[0]:
                return 0
            ids = data[0].split()
            return int(ids[-1]) if ids else 0
        except Exception:
            return 0

    def fetch(self, uid):
        self.connect()
        try:
            typ, data = self._imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        except Exception as e:
            raise MailError("IMAP fetch %s failed: %s" % (uid, e))
        if typ != "OK" or not data:
            return None
        raw = b""
        for part in data:
            if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                raw = part[1]
                break
        if not raw:
            return None
        msg = email.message_from_bytes(raw)
        text_parts, html_parts = [], []
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if part.get_content_maintype() == "multipart":
                continue
            if ctype == "text/plain":
                text_parts.append(_decode_part(part))
            elif ctype == "text/html":
                html_parts.append(_decode_part(part))
        date = None
        try:
            date = email.utils.parsedate_to_datetime(msg.get("Date") or "")
        except Exception:
            pass
        return Message(uid,
                       subject=_header(msg, "Subject"),
                       sender=_header(msg, "From"),
                       date=date,
                       text="\n".join(t for t in text_parts if t),
                       html="\n".join(h for h in html_parts if h))

    def messages(self, **kw):
        limit = kw.pop("limit", 20)
        return [m for m in (self.fetch(u) for u in self.uids(limit=limit, **kw)) if m]

    # ── the two things the registration loop asks for ─────────────────────────
    def wait_for_code(self, since_uid=0, timeout=None, hints=(), sender=None,
                      subject=None):
        """Poll until a letter after `since_uid` carries a code. (msg, code)|None."""
        return self._wait(since_uid, timeout, sender, subject,
                          lambda m: (m.code(hints) or self._regex_code(m)))

    def wait_for_link(self, since_uid=0, timeout=None, allow_hosts=(), sender=None,
                      subject=None):
        """Poll until a letter after `since_uid` carries a confirmation link."""
        return self._wait(since_uid, timeout, sender, subject,
                          lambda m: (m.links(allow_hosts) or [None])[0])

    def _regex_code(self, msg):
        """Escape hatch: a site with an unusual code format sets `code_regex`."""
        if not self.code_regex:
            return None
        m = re.search(self.code_regex, msg.body, re.I)
        return m.group(1) if m and m.groups() else (m.group(0) if m else None)

    def _wait(self, since_uid, timeout, sender, subject, extract):
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        seen = set()
        while True:
            for uid in self.uids(since_uid=since_uid, sender=sender,
                                 subject=subject, limit=10):
                if uid in seen:
                    continue
                seen.add(uid)
                msg = self.fetch(uid)
                if not msg:
                    continue
                value = extract(msg)
                if value:
                    return msg, value
            if time.time() >= deadline:
                return None
            time.sleep(self.poll)
