"""signup.py — one page, one decision, one action, repeat.

Registration is not a script, it is a *policy*. Every site words its form
differently: `E-mail` / `Почта` / `Электронный адрес`, the button says
"Создать аккаунт" or "Продолжить", and step two is sometimes a code from
mail, sometimes a captcha, sometimes a phone call. A hard-coded selector list
works until the first redesign; a model that reads the page and picks the next
action survives it.

So the loop is deliberately dumb, and all the judgement lives in one call:

    snapshot -> decide -> act -> snapshot -> ...

Three things keep it honest:

* **rules first, model second.** `heuristic()` handles the ordinary case
  (empty required fields, one submit button) with no API call at all. The
  model is asked only when the page is ambiguous — same philosophy as
  meta.py: settle what the table can settle, spend a call on the rest.
* **no captcha, no SMS, no payment.** Those turn into `blocked`, which is a
  message to the owner, not a puzzle for the agent. Solving a captcha is both
  a ToS violation and a losing race; passing an SMS code is impossible without
  the phone.
* **every step is written down.** `.ai_pobisk/signup/<site>.json` holds the
  full trace: what the page looked like, what was decided, what happened. An
  autonomous account creation that cannot be audited afterwards is a liability.

Placeholders (`{{email}}`, `{{password}}`, `{{code}}`) are resolved by the
executor, never by the model: the model decides *where* a value goes, the
program decides *what* the value is. That is also why the password never
appears in a prompt.
"""

import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_HERE, "adapters"),
           os.path.join(_ROOT, ".agents", "skills", "_shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pageview                                    # noqa: E402

ACTIONS = {"fill", "click", "check", "uncheck", "select", "press", "goto",
           "wait", "wait_email", "open_email_link", "done", "blocked"}

SYSTEM_PROMPT = """You drive a web form for a human. You see a compact model of
the page: numbered controls ([f1] input:email "Почта" name=email req),
the page text, and the history of what was already tried.

Answer with JSON only, no prose:
{"action": "fill|click|check|uncheck|select|press|goto|wait|wait_email|open_email_link|done|blocked",
 "target": "f3", "value": "text", "reason": "one short line", "confidence": 0.0}

Values you may use as placeholders: {{email}} {{password}} {{name}} {{surname}}
{{login}} {{phone}} {{birthdate}} {{city}} {{code}} {{link}}.
Rules:
- One action per step. Fill the first empty required control, then the next.
- Use {{password}} for password fields; never invent a password.
- Never invent a code. If the page asks for a code from e-mail: first
  "wait_email", then fill {{code}} in the code field.
- If the letter asks to confirm an address by link: "open_email_link".
- If the page shows a captcha, an SMS code, a phone call or a payment —
  answer "blocked" with the reason. Do not attempt to pass it.
- If the account is created (success text, "check your email", a logged-in
  page) answer "done".
- Read HISTORY: never repeat an action that already failed. If the page did
  not change after your last action, try something different.
- Prefer the smallest step that makes progress."""

# What a field is, judged by its label/name/type. Order matters: the first
# match wins, so "password_confirm" is a password before it is a "confirm".
FIELD_HINTS = [
    ("password", ("password", "passwd", "пароль", "pass")),
    ("email", ("email", "e-mail", "почт", "mail")),
    ("login", ("login", "username", "nickname", "логин", "ник", "user")),
    ("phone", ("phone", "tel", "мобильн", "телефон")),
    ("name", ("first_name", "firstname", "given", "имя")),
    ("surname", ("last_name", "lastname", "surname", "family", "фамили")),
    ("birthdate", ("birth", "дата рожден", "день рожден")),
    ("city", ("city", "город")),
]

SUBMIT_WORDS = ("зарегистр", "создать", "регистрац", "продолжить", "далее",
                "подтверд", "отправить", "sign up", "signup", "register",
                "create", "continue", "next", "submit", "verify", "confirm")


def _json_from(text):
    """First JSON object in a model answer. Models love prose around JSON."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


# ── who we are ────────────────────────────────────────────────────────────────
class Profile:
    """The facts a form may ask for. Loaded from configs/profile.toml."""

    KEYS = ("email", "name", "surname", "login", "phone", "birthdate", "city",
            "country", "password", "company", "site")

    def __init__(self, data=None, path=""):
        self.data = dict(data or {})
        self.path = path
        self.extra = dict(self.data.get("extra") or {})
        self.overrides = dict(self.data.get("sites") or {})

    @classmethod
    def load(cls, path):
        if not path or not os.path.exists(path):
            return cls()
        try:
            import tomllib
            with open(path, "rb") as f:
                return cls(tomllib.load(f), path)
        except Exception:
            return cls({}, path)

    def for_site(self, site):
        """Per-site overrides win over the general profile."""
        merged = dict(self.data)
        over = (self.overrides.get(site) or {})
        merged.update({k: v for k, v in over.items() if k not in ("extra", "sites")})
        extra = dict(self.extra)
        extra.update(over.get("extra") or {})
        out = Profile(merged, self.path)
        out.extra = extra
        out.overrides = self.overrides
        return out

    def value(self, key):
        return self.data.get(key) or self.extra.get(key) or ""

    def lines(self, mask_password=True):
        out = []
        for k in self.KEYS:
            v = self.data.get(k)
            if v in (None, ""):
                continue
            if k == "password" and mask_password:
                v = "***"
            out.append("%s: %s" % (k, v))
        for k, v in self.extra.items():
            out.append("%s: %s" % (k, v))
        return out


def _field_kind(node):
    hay = " ".join([node.name or "", node.label or "", node.type or ""]).lower()
    for key, words in FIELD_HINTS:
        if any(w in hay for w in words):
            return key
    return ""


def _value_for(node, profile, ctx=None):
    """The value for a field: the run's context first, the profile second.

    Context matters because the password may be *generated* for this run and
    therefore exists nowhere in the profile.
    """
    kind = _field_kind(node)
    if kind:
        val = (ctx or {}).get(kind) or profile.value(kind)
        if val:
            return val
    # An exact match on a custom key ("inn", "company") is better than nothing.
    for key, val in list(profile.data.items()) + list(profile.extra.items()):
        if key in ("password", "sites", "extra") or not isinstance(val, str):
            continue
        if key.lower() in (node.name or "").lower() and val:
            return val
    return ""


class Thinker:
    """Decides the next action: rules first, a model only when needed."""

    def __init__(self, repo_root, cfg, adapters=None, log=print):
        self.repo_root = repo_root
        self.cfg = cfg
        self.conf = cfg.get("signup") or {}
        self.adapters = adapters
        self.log = log
        self.calls = 0
        self.model = None
        self._dead = False

    # ── candidate models ──────────────────────────────────────────────────────
    def _candidates(self):
        cands = self.conf.get("candidates") or self.cfg["meta"].get("candidates") or []
        return cands

    def _ask(self, prompt):
        """One call to the first candidate that answers. None means 'use rules'."""
        if self._dead or self.adapters is None:
            return None
        limit = int(self.conf.get("max_llm_calls") or 25)
        if self.calls >= limit:
            self._dead = True
            return None
        for cand in self._candidates():
            skill, model = cand.get("skill", ""), cand.get("model", "")
            a = self.adapters.get(skill)
            if not a or not a.available():
                continue
            self.calls += 1
            o = a.run(prompt, model=model,
                      timeout=int(self.conf.get("llm_timeout_sec") or 60))
            if not o.ok and not o.raw:
                continue
            parsed = _json_from(o.raw)
            if parsed:
                self.model = (skill, model)
                return parsed
            self.log("  model %s/%s returned no JSON (%s)" % (skill, model, o.outcome))
        self._dead = True
        return None

    # ── the decision ──────────────────────────────────────────────────────────
    def decide(self, view, history, profile, ctx):
        rule = heuristic(view, profile, ctx)
        if rule is not None:
            return rule
        prompt = self._prompt(view, history, profile)
        answer = self._ask(prompt)
        if not answer:
            return {"action": "blocked", "target": "", "value": "",
                    "reason": "no model and the page has no obvious next step",
                    "confidence": 0.0}
        action = str(answer.get("action") or "").strip().lower()
        if action not in ACTIONS:
            return {"action": "blocked", "target": "", "value": "",
                    "reason": "model proposed unknown action %r" % action,
                    "confidence": 0.0}
        target = str(answer.get("target") or "").strip()
        if action in ("fill", "click", "check", "uncheck", "select", "press") \
                and not view.by_ref(target):
            # A hallucinated ref is worse than a pause: it clicks nothing.
            return {"action": "blocked", "target": target, "value": "",
                    "reason": "model named a control that is not on the page (%s)"
                              % target, "confidence": 0.0}
        try:
            conf = float(answer.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        floor = float(self.conf.get("min_confidence") or 0.35)
        if conf < floor:
            return {"action": "blocked", "target": target,
                    "value": str(answer.get("value") or ""),
                    "reason": "model is unsure (%.2f): %s" % (conf, answer.get("reason", "")),
                    "confidence": conf}
        return {"action": action, "target": target,
                "value": str(answer.get("value") or ""),
                "reason": str(answer.get("reason") or "")[:200],
                "confidence": conf}

    def _prompt(self, view, history, profile):
        hist = "\n".join(history[-12:]) or "(nothing yet)"
        return ("%s\n\nPROFILE\n%s\n\nHISTORY\n%s\n\nPAGE\n%s"
                % (SYSTEM_PROMPT, "\n".join(profile.lines()) or "(empty)",
                   hist, view.to_prompt()))


# ── rules, so the loop works with no model at all ─────────────────────────────
def heuristic(view, profile, ctx):
    """A deterministic next step, or None when the page needs judgement.

    This is what makes `--dry-run` meaningful and what keeps a broken model
    from stopping a registration that a human would finish in ten seconds.
    """
    for node in view.fields:
        if node.disabled or node.tag == "select":
            continue
        if node.type == "checkbox":
            if not node.checked and (node.required or view.has_flag("agreement")):
                if ctx.get("accept_terms", True):
                    return {"action": "check", "target": node.ref, "value": "",
                            "reason": "required agreement checkbox",
                            "confidence": 0.8}
            continue
        if node.type == "radio":
            continue
        if node.value:
            continue
        value = _value_for(node, profile, ctx)
        if value:
            return {"action": "fill", "target": node.ref, "value": value,
                    "reason": "empty %s field" % (_field_kind(node) or "text"),
                    "confidence": 0.7}
        if node.required:
            return None                 # required and unknown: ask the model
        # Optional and unknown ("referral code", "how did you hear about us"):
        # leave it empty. Asking the model here bought nothing but a wrong
        # click on xkiro.com — it answered "press the registration tab".

    for node in view.fields:
        if node.tag == "select" and not node.value:
            if node.options:
                return {"action": "select", "target": node.ref,
                        "value": node.options[0],
                        "reason": "select has no value", "confidence": 0.5}
            return None

    # Everything is filled: press the button that means "create".
    target = _pick_submit(view)
    if target is not None:
        if ctx.get("submitted"):
            return None                 # pressing again would be a double submit
        return {"action": "click", "target": target.ref, "value": "",
                "reason": "form is filled, press %r" % (target.label or target.ref),
                "confidence": 0.7}
    return None


def _pick_submit(view):
    """The button that submits the form we just filled.

    Text alone is not enough: xkiro.com has a tab labelled "Регистрация" right
    above the real submit, and the first version of this function clicked the
    tab. A tab lives outside the <form>; a submit lives inside it, so the form
    we filled is the signal, and the label only breaks ties.
    """
    buttons = [n for n in view.buttons if not n.disabled]
    if not buttons:
        return None
    forms = [f.form for f in view.fields if f.value and f.form]
    mine = max(set(forms), key=forms.count) if forms else 0

    def rank(n):
        return (0 if mine and n.form == mine else 1,
                0 if n.type == "submit" else 1,
                0 if any(w in (n.label or "").lower() for w in SUBMIT_WORDS) else 1)

    return sorted(buttons, key=rank)[0]


def site_of(url):
    """Host without www, dots to dashes: the key for profile overrides."""
    m = re.match(r"https?://([^/:?#]+)", url or "", re.I)
    return (m.group(1) if m else "site").lower().replace("www.", "")


# ── the loop ──────────────────────────────────────────────────────────────────
class Signup:
    def __init__(self, repo_root, cfg, driver, thinker=None, mail=None,
                 profile=None, log=print):
        self.repo_root = repo_root
        self.cfg = cfg
        self.conf = cfg.get("signup") or {}
        self.driver = driver
        self.thinker = thinker or Thinker(repo_root, cfg, log=log)
        self.mail = mail
        self.profile = profile or Profile()
        self.log = log
        self.history = []
        self.trace = []
        self.state = {"submitted": False,
                      "accept_terms": bool(self.conf.get("accept_terms", True))}

    # ── helpers ───────────────────────────────────────────────────────────────
    def _site(self, url):
        return site_of(url)

    def _ctx(self, url):
        site = self._site(url)
        prof = self.profile.for_site(site)
        ctx = {k: prof.value(k) for k in Profile.KEYS}
        ctx.update(prof.extra)
        ctx["site"] = site
        ctx["email"] = ctx.get("email") or self.conf.get("email") or ""
        ctx["login"] = ctx.get("login") or ctx["email"]
        ctx["password"] = ctx.get("password") or self.conf.get("password") or ""
        if not ctx["password"]:
            ctx["password"] = _gen_password()
        ctx["accept_terms"] = self.state["accept_terms"]
        return ctx

    def _resolve(self, value, ctx):
        if not value:
            return value
        out = str(value)
        for key, val in ctx.items():
            if isinstance(val, (str, int, float)):
                out = out.replace("{{%s}}" % key, str(val))
        return out

    def _blocker(self, view):
        """Things an agent must hand back to the owner instead of solving."""
        if not self.conf.get("stop_on_blockers", True):
            return ""
        empty = [f for f in view.fields if not f.value and f.type != "checkbox"]
        if view.has_flag("captcha_widget") and not empty:
            return "captcha"
        if view.has_flag("payment") and not empty:
            return "payment"
        if view.has_flag("sms"):
            for f in view.fields:
                label = (f.label or "").lower()
                if "код" in label or "code" in label:
                    return "sms"
        return ""

    def _note(self, text):
        self.log("  " + text)
        self.history.append(text)

    # ── main ──────────────────────────────────────────────────────────────────
    def run(self, url, dry_run=False, max_steps=None, allow_live=False,
            save_password=False):
        max_steps = int(max_steps or self.conf.get("max_steps") or 40)
        # `or` would turn an explicit 0 into 600; a zero budget is a valid
        # (if useless) configuration and must be honoured.
        wall_conf = self.conf.get("max_wall_clock_sec")
        wall = float(600 if wall_conf is None else wall_conf)
        started = time.time()
        ctx = self._ctx(url)
        site = ctx["site"]
        live = allow_live and not dry_run
        # Shared, not copied: the heuristic must see that a submit already
        # happened, so it does not press the same button twice.
        ctx["submitted"] = False

        if not ctx["email"]:
            return self._finish("error", site, url, "profile has no e-mail address")
        if self.mail is not None:
            try:
                ctx["since_uid"] = self.mail.last_uid()
            except Exception as e:
                self._note("mailbox unavailable: %s" % e)
                self.mail = None

        try:
            self.driver.goto(url)
        except Exception as e:
            return self._finish("error", site, url, "cannot open page: %s" % e)

        repeats = {}
        last_sig, stuck = "", 0
        for step in range(1, max_steps + 1):
            if time.time() - started > wall:
                return self._finish("budget", site, url,
                                    "wall clock exceeded after %d steps" % (step - 1))
            try:
                view = pageview.parse(self.driver.html(), url=self.driver.url())
            except Exception as e:
                return self._finish("error", site, url, "snapshot failed: %s" % e)

            # "Проверьте почту" is not the finish line when a code field is
            # still empty: the account exists, the address is unconfirmed.
            unfilled = [f for f in view.fields if not f.value and f.type != "checkbox"]
            if pageview.is_success(view) and step > 1 and not unfilled:
                return self._finish("registered", site, url,
                                    "success page: %s" % view.title, ctx=ctx,
                                    save_password=save_password)

            blocker = self._blocker(view)
            if blocker:
                return self._finish("blocked", site, url,
                                    "page requires %s — needs a human" % blocker,
                                    ctx=ctx)

            action = self.thinker.decide(view, self.history, self.profile, ctx)
            act = action["action"]
            target = action.get("target", "")
            value = self._resolve(action.get("value", ""), ctx)
            reason = action.get("reason", "")

            if act == "blocked":
                # The page usually explains itself; the model's paraphrase does
                # not. Keep both, page text last so it reads as evidence.
                if view.errors:
                    reason = "%s | page: %s" % (reason or "model blocked",
                                                "; ".join(view.errors[:2]))
                return self._finish("blocked", site, url, reason or "model blocked",
                                    ctx=ctx)
            if act == "done":
                return self._finish("registered", site, url,
                                    reason or "model reports success", ctx=ctx,
                                    save_password=save_password)
            # An unresolved placeholder means the value is not known yet — the
            # model asked to type "{{code}}" before any letter arrived.
            if "{{" in str(value):
                return self._finish("blocked", site, url,
                                    "unresolved placeholder in value: %s" % value,
                                    ctx=ctx)
            if not live:
                self.log("  [dry-run] %s %s %r  (%s)" % (act, target, value, reason))
                return self._finish("dry_run", site, url,
                                    "planned: %s %s" % (act, target), ctx=ctx)

            key = (act, target, value)
            repeats[key] = repeats.get(key, 0) + 1
            if repeats[key] >= 3:
                return self._finish("loop_guard", site, url,
                                    "same action three times: %s %s" % (act, target),
                                    ctx=ctx)
            sig = view.signature()
            stuck = stuck + 1 if sig == last_sig else 0
            last_sig = sig
            if stuck >= 4:
                return self._finish("loop_guard", site, url,
                                    "page did not change for %d steps" % stuck, ctx=ctx)

            # The history is fed back into the prompt and the trace is kept on
            # disk: a password must not appear in either.
            shown = "***" if value and value == ctx.get("password") else value
            self._note("%d. %s %s %r -> %s"
                       % (step, act, target, shown[:30], reason[:60]))
            self.trace.append({"step": step, "action": act, "target": target,
                               "value": shown, "reason": reason, "url": view.url,
                               "title": view.title, "flags": view.flags})

            try:
                outcome = self._execute(act, target, value, ctx, view)
            except Exception as e:
                return self._finish("error", site, url,
                                    "%s %s failed: %s" % (act, target, e), ctx=ctx)
            if outcome == "no_code":
                return self._finish("blocked", site, url,
                                    "letter with a code did not arrive in time",
                                    ctx=ctx)
            if act == "click":
                self.state["submitted"] = True
                ctx["submitted"] = True

        return self._finish("budget", site, url,
                            "step limit %d reached" % max_steps, ctx=ctx)

    def _execute(self, act, target, value, ctx, view):
        d = self.driver
        if act == "fill":
            d.fill(target, value)
            node = view.by_ref(target)
            if node is not None:
                node.value = value            # keep the local model in sync
        elif act == "click":
            d.click(target)
        elif act == "check":
            d.check(target, True)
        elif act == "uncheck":
            d.check(target, False)
        elif act == "select":
            d.select(target, value)
        elif act == "press":
            d.press(target or None, value or "Enter")
        elif act == "goto":
            d.goto(value)
        elif act == "wait":
            d.wait(int(float(value or 2) * 1000))
        elif act == "wait_email":
            return self._read_mail(ctx, want="code")
        elif act == "open_email_link":
            return self._read_mail(ctx, want="link")
        else:
            raise ValueError("unknown action %r" % act)
        return "ok"

    def _read_mail(self, ctx, want="code"):
        if self.mail is None:
            raise RuntimeError("no mailbox configured ([mail] in configs/ai_pobisk.toml)")
        since = int(ctx.get("since_uid") or 0)
        timeout = float(self.conf.get("mail_timeout_sec") or 180)
        if want == "code":
            got = self.mail.wait_for_code(since_uid=since, timeout=timeout,
                                          hints=ctx.get("code_hints") or ())
        else:
            got = self.mail.wait_for_link(since_uid=since, timeout=timeout,
                                          allow_hosts=[ctx["site"]])
        if not got:
            self._note("no letter within %ss" % timeout)
            return "no_code"
        msg, value = got
        ctx["since_uid"] = int(msg.uid)
        ctx["code" if want == "code" else "link"] = value
        self._note("mail: %r -> %s" % (msg.subject[:50], value[:60]))
        if want == "link":
            self.driver.goto(value)
        return "ok"

    # ── result ────────────────────────────────────────────────────────────────
    def _finish(self, status, site, url, reason, ctx=None, save_password=False):
        ctx = ctx or {}
        result = {"status": status, "site": site, "url": url, "reason": reason,
                  "steps": len(self.trace), "model": self.thinker.model,
                  "llm_calls": getattr(self.thinker, "calls", 0),
                  "login": ctx.get("login", ""), "email": ctx.get("email", "")}
        if status == "registered":
            self._save_account(result, ctx, save_password)
        self._save_trace(site, result)
        self._journal(result)
        return result

    def _save_account(self, result, ctx, save_password):
        if not result.get("login"):
            return
        rec = {"site": result["site"], "url": result["url"],
               "login": ctx.get("login", ""), "email": ctx.get("email", ""),
               "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        if save_password:
            name = "%s_%s" % (result["site"].replace(".", "-"),
                              re.sub(r"[^a-z0-9]+", "-", result["login"].lower())[:24])
            if _store_password(name, ctx.get("password", "")):
                rec["password_secret"] = name
        path = os.path.join(self.repo_root, self.cfg["state"]["dir"], "accounts.json")
        data = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = []
        data.append(rec)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_trace(self, site, result):
        d = os.path.join(self.repo_root, self.cfg["state"]["dir"], "signup")
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "%s-%s.json" % (site.replace(".", "-"),
                                                   time.strftime("%Y%m%d-%H%M%S")))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"result": result, "steps": self.trace,
                           "history": self.history}, f, ensure_ascii=False, indent=2)
            result["trace"] = path
        except Exception:
            pass

    def _journal(self, result):
        try:
            import journal as journal_mod
            j = journal_mod.Journal(self.repo_root, self.cfg)
            j.event("signup %s: %s (%s)" % (result["site"], result["status"],
                                            result["reason"]),
                    etype="signup", site=result["site"], status=result["status"])
        except Exception:
            pass


def _gen_password(length=16):
    """Random password from an alphabet with no look-alike characters."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%"
    out = []
    while len(out) < length:
        b = os.urandom(1)[0]
        if b < 256 - (256 % len(alphabet)):
            out.append(alphabet[b % len(alphabet)])
    # Guarantee one of each class, so a strict password policy passes.
    out[0] = "A"
    out[1] = "a"
    out[2] = "7"
    out[3] = "!"
    return "".join(out[:length])


def _store_password(name, password):
    """Save to the shared 0600 store, never to the repo."""
    if not password:
        return False
    try:
        from mailbox import _shared_secrets
        mod = _shared_secrets()
        if not mod:
            return False
        data = mod.load() or {}
        data[name] = {"key": password, "label": "signup", "added": int(time.time())}
        mod.save(data)
        return True
    except Exception:
        return False
