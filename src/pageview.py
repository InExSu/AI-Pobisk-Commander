"""pageview.py — a page as a model can read it, and a driver can act on.

The agent loop needs a *stable vocabulary* between "look at the page" and
"click the thing". Screenshots are the wrong tool: they cost thousands of
tokens, they are not deterministic, and a click cannot be aimed at a pixel
reliably. A DOM dump is the opposite problem — 300 kB of markup where the
signal (six fields and two buttons) is 1%.

So: parse the page into a small numbered list of controls, and give every
control a `ref`. The driver tags the live DOM with the same refs before
snapshotting, so `[f2]` in the prompt is exactly `[data-pb-ref="f2"]` in the
browser — no selector guessing, no "click the third div with class btn".

Two properties worth keeping:

* **deterministic.** The same HTML always yields the same view, so the loop
  can be tested without a browser at all (see test/test_pageview.py).
* **honest about danger.** `flags` names the things an agent must *not* push
  through: captcha, SMS, payment. The loop turns those into "ask the owner"
  instead of a creative attempt.
"""

import hashlib
import re
from html.parser import HTMLParser

SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
FIELD_TAGS = {"input", "select", "textarea"}
INTERACTIVE = FIELD_TAGS | {"button", "a"}

# Input types that carry no meaning for a human and no value for a bot.
IGNORED_INPUT_TYPES = {"hidden", "image", "reset", "file", "range", "color"}

FLAG_PATTERNS = {
    "captcha": r"recaptcha|hcaptcha|turnstile|cf-challenge|я не робот|"
               r"not a robot|captcha",
    "sms": r"код из смс|sms code|подтверд\w* телефон|verify your phone|"
           r"phone verification|введите код из сообщения",
    "payment": r"номер карты|card number|оплат[аиу]|payment required|"
               r"привяз\w* карт",
    "agreement": r"принимаю|согласен|terms of service|пользовательское "
                 r"соглашение|оферт|privacy policy",
    "success": r"спасибо за регистрац|успешн\w* регистрац|проверьте почту|"
               r"check your (inbox|email|mail)|добро пожаловать|welcome|"
               r"account (was )?created|аккаунт создан",
    "error": r"уже зарегистрирован|already registered|неверн\w*|invalid|"
             r"ошибка|error|занят",
}

_SPACE_RE = re.compile(r"\s+")


def _clean(s, limit=None):
    s = _SPACE_RE.sub(" ", (s or "")).strip()
    return s[:limit] if limit else s


class Node:
    """One control the agent may touch."""

    __slots__ = ("ref", "tag", "type", "name", "label", "value", "required",
                 "options", "href", "checked", "disabled", "text", "form")

    def __init__(self, ref, tag, type_="", name="", label="", value="",
                 required=False, href="", checked=False, disabled=False,
                 form=0):
        self.ref = ref
        self.tag = tag
        self.type = type_
        self.name = name
        self.label = label
        self.value = value
        self.required = required
        self.options = []
        self.href = href
        self.checked = checked
        self.disabled = disabled
        self.text = ""
        # Index of the nearest <form>, 0 = outside any form. A tab labelled
        # "Регистрация" and the real submit can share a label; the form they
        # belong to is what tells them apart.
        self.form = form

    @property
    def kind(self):
        # "input:email" rather than "email": the model must not confuse a
        # field type with a control kind when it picks a ref.
        if self.tag == "input":
            return "input:%s" % (self.type or "text")
        return self.tag

    def describe(self, max_label=60):
        bits = ["[%s]" % self.ref, self.kind]
        label = _clean(self.label or self.name, max_label)
        if label:
            bits.append('"%s"' % label)
        if self.name:
            bits.append("name=%s" % self.name)
        if self.required:
            bits.append("req")
        if self.checked:
            bits.append("checked")
        if self.disabled:
            bits.append("disabled")
        if self.value and self.tag != "a":
            shown = "***" if self.type == "password" else _clean(self.value, 40)
            bits.append('value="%s"' % shown)
        if self.options:
            bits.append("options=[%s]" % "|".join(
                _clean(o, 24) for o in self.options[:12]))
        if self.tag == "a" and self.href:
            bits.append("href=%s" % _clean(self.href, 80))
        return " ".join(bits)


class PageView:
    """The parsed page: controls, flags, a little text."""

    def __init__(self, url="", title="", nodes=None, text="", flags=None,
                 errors=None):
        self.url = url or ""
        self.title = _clean(title, 120)
        self.nodes = nodes or []
        self.text = text or ""
        self.flags = flags or []
        # Validation messages are the only feedback a form ever gives. The
        # model must see them verbatim: on xkiro.com "только @gmail.com" was
        # the whole answer, and without it the model invented a reason.
        self.errors = errors or []

    def by_ref(self, ref):
        for n in self.nodes:
            if n.ref == ref:
                return n
        return None

    @property
    def fields(self):
        return [n for n in self.nodes if n.tag in FIELD_TAGS and
                n.type not in ("submit", "button")]

    @property
    def buttons(self):
        return [n for n in self.nodes if n.tag == "button" or
                (n.tag == "input" and n.type in ("submit", "button"))]

    @property
    def links(self):
        return [n for n in self.nodes if n.tag == "a"]

    def has_flag(self, name):
        return name in self.flags

    def signature(self):
        """Cheap fingerprint: same controls with same values = same page."""
        raw = "|".join("%s=%s" % (n.ref, n.value) for n in self.nodes)
        raw += "|" + self.url + "|" + self.title
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]

    def to_prompt(self, max_chars=3200, max_fields=40, max_links=10):
        """Compact, line-per-control text. This is what the model sees."""
        lines = ["URL: %s" % self.url, "TITLE: %s" % (self.title or "-")]
        if self.flags:
            lines.append("FLAGS: %s" % ", ".join(self.flags))
        if self.errors:
            lines.append("ERRORS: %s" % " | ".join(self.errors[:3]))
        lines.append("CONTROLS:")
        shown_fields = shown_links = 0
        for n in self.nodes:
            if n.tag == "a":
                if shown_links >= max_links:
                    continue
                shown_links += 1
            else:
                if shown_fields >= max_fields:
                    continue
                shown_fields += 1
            lines.append(n.describe())
        text = _clean(self.text)
        if text:
            lines.append("TEXT: " + text)
        out = "\n".join(lines)
        if len(out) > max_chars:
            out = out[:max_chars].rsplit("\n", 1)[0] + "\n... (page text truncated)"
        return out


class _Parser(HTMLParser):
    def __init__(self, url="", max_text=1500):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.max_text = max_text
        self.nodes = []
        self.text_parts = []
        self.title_parts = []
        self.labels_for = {}          # id -> text (from <label for=...>)
        self.open_labels = []         # [{"for": id, "parts": [...], "field": Node}]
        self.capture = []             # [(Node, [text parts])] for button/a/option
        self.select = None
        self.errors = []
        self.error_stack = []
        self.form_index = 0
        self.form_stack = []
        self.skip_depth = 0
        self.in_title = False
        self.auto = 0
        self.used_refs = set()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _ref(self, attrs):
        given = (attrs.get("data-pb-ref") or "").strip()
        if given and given not in self.used_refs:
            self.used_refs.add(given)
            return given
        self.auto += 1
        while "f%d" % self.auto in self.used_refs:
            self.auto += 1
        ref = "f%d" % self.auto
        self.used_refs.add(ref)
        return ref

    @staticmethod
    def _is_error_box(tag, attrs):
        if attrs.get("role") in ("alert", "status"):
            return True
        if attrs.get("aria-live") in ("assertive", "polite"):
            return True
        cls = (attrs.get("class") or "").lower()
        return bool(re.search(r"error|invalid|danger|destructive|text-red|"
                              r"form-message|field-error", cls))

    @staticmethod
    def _on(attrs, key):
        # A boolean attribute is present or absent; HTMLParser yields None for
        # `<input required>`, and "" after the (v or "") normalisation above.
        if key not in attrs:
            return False
        return (attrs.get(key) or "").lower() not in ("false", "0")

    def _add(self, node):
        self.nodes.append(node)
        if self.open_labels and node.tag in FIELD_TAGS:
            label = self.open_labels[-1]
            if label["field"] is None:
                label["field"] = node

    # ── HTMLParser hooks ──────────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs_list):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        attrs = {k.lower(): (v or "") for k, v in attrs_list}

        if tag == "title":
            self.in_title = True
            return
        if tag == "form":
            self.form_index += 1
            self.form_stack.append(self.form_index)
            return
        if tag == "label":
            self.open_labels.append({"for": attrs.get("for", ""),
                                     "parts": [], "field": None})
            return
        if self._is_error_box(tag, attrs):
            self.error_stack.append({"tag": tag, "parts": []})
            return
        if attrs.get("data-pb-offscreen") == "1":
            return

        if tag in FIELD_TAGS:
            itype = (attrs.get("type") or "text").lower()
            if tag == "input" and itype in IGNORED_INPUT_TYPES:
                return
            node = Node(
                form=self.form_stack[-1] if self.form_stack else 0,
                ref=self._ref(attrs),
                tag=tag,
                type_=itype if tag == "input" else tag,
                name=attrs.get("name", ""),
                label=attrs.get("aria-label") or attrs.get("placeholder")
                      or attrs.get("title") or "",
                value=attrs.get("value", "") or attrs.get("data-pb-value", ""),
                required=self._on(attrs, "required") or
                         attrs.get("aria-required") == "true",
                checked=self._on(attrs, "checked"),
                disabled=self._on(attrs, "disabled"),
            )
            if attrs.get("id") and attrs["id"] in self.labels_for:
                node.label = node.label or self.labels_for[attrs["id"]]
            self._add(node)
            if tag == "select":
                self.select = node
            elif tag == "textarea":
                self.capture.append((node, []))
            return

        if tag == "button":
            node = Node(form=self.form_stack[-1] if self.form_stack else 0,
                        ref=self._ref(attrs), tag="button",
                        type_=(attrs.get("type") or "submit").lower(),
                        name=attrs.get("name", ""),
                        label=attrs.get("aria-label", ""),
                        disabled=self._on(attrs, "disabled"))
            self._add(node)
            self.capture.append((node, []))
            return

        if tag == "a" and attrs.get("href"):
            href = attrs["href"].strip()
            if href.lower().startswith(("javascript:", "#")):
                return
            node = Node(form=self.form_stack[-1] if self.form_stack else 0,
                        ref=self._ref(attrs), tag="a", type_="link",
                        label=attrs.get("aria-label", ""), href=href)
            self._add(node)
            self.capture.append((node, []))
            return

        if tag == "option" and self.select is not None:
            label = _clean(attrs.get("label") or "", 40)
            value = attrs.get("value", "")
            self.select.options.append(value or label)
            self.capture.append((None, []))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.error_stack and self.error_stack[-1]["tag"] == tag:
            text = _clean("".join(self.error_stack.pop()["parts"]), 160)
            if text and text not in self.errors:
                self.errors.append(text)
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
            return
        if tag == "form":
            if self.form_stack:
                self.form_stack.pop()
            return
        if tag == "label" and self.open_labels:
            lab = self.open_labels.pop()
            text = _clean("".join(lab["parts"]), 80)
            if lab["for"]:
                self.labels_for.setdefault(lab["for"], text)
                for n in self.nodes:
                    if n.name == lab["for"] and not n.label:
                        n.label = text
            if lab["field"] is not None and not lab["field"].label:
                lab["field"].label = text
            return
        if tag == "option":
            if self.capture and self.capture[-1][0] is None:
                self.capture.pop()
            return
        if tag in ("button", "a") and self.capture:
            node, parts = self.capture.pop()
            if node is not None:
                text = _clean("".join(parts), 80)
                if text:
                    node.text = text
                    node.label = node.label or text
            return
        if tag == "textarea" and self.capture:
            node, parts = self.capture.pop()
            if node is not None and not node.value:
                node.value = _clean("".join(parts), 60)
            return
        if tag == "select":
            self.select = None

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.error_stack:
            self.error_stack[-1]["parts"].append(data)
        if self.open_labels:
            self.open_labels[-1]["parts"].append(data)
        if self.capture:
            node, parts = self.capture[-1]
            parts.append(data)
        if sum(len(p) for p in self.text_parts) < self.max_text * 2:
            self.text_parts.append(data)


def parse(html, url="", max_text=1500, max_nodes=60, max_links=15):
    """HTML -> PageView. Never raises: a broken page yields an empty view."""
    p = _Parser(url=url, max_text=max_text)
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass

    nodes, links = [], 0
    for n in p.nodes:
        if n.tag == "a":
            links += 1
            if links > max_links:
                continue
        nodes.append(n)
    nodes = nodes[:max_nodes + max_links]

    text = _clean("".join(p.text_parts))[:max_text]
    low = ((html or "")[:400000] + " " + text).lower()
    flags = [name for name, pat in FLAG_PATTERNS.items() if re.search(pat, low)]
    # `captcha` means "a captcha script is loaded"; `captcha_widget` means
    # "a captcha is actually on screen asking for a click". Only the second
    # one is a reason to stop: v3/invisible widgets pass on their own, and
    # blocking on the script alone would refuse half the modern web.
    if 'data-pb-captcha="visible"' in (html or ""):
        flags.append("captcha_widget")
    return PageView(url=url, title="".join(p.title_parts), nodes=nodes,
                    text=text, flags=flags, errors=p.errors[:5])


def is_success(view):
    return bool(view.has_flag("success")) and not view.has_flag("error")
