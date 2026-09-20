"""meta.py — ai_Pobisk's own reasoning model.

The meta model is asked only when the deterministic rule table has no answer:
an unrecognised error, an exhausted retry budget, or a question whose
competence is unclear. Everything the rule table can settle, it settles
without spending a call.

Critical property: if the meta model is unreachable, ai_Pobisk degrades to
`rules_only` instead of freezing. A supervisor that stops because its own
model is down is worse than one that keeps going on rules.
"""

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, os.path.join(_HERE, "adapters"),
          os.path.join(_ROOT, ".agents", "skills", "_shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

from outcome import Outcome, OK, RETRY, ROTATE_MODEL, ROTATE_ACCOUNT, FATAL, LOOP_GUARD  # noqa: E402

SYSTEM_PROMPT = """You are the decision layer of ai_Pobisk, a supervisor that runs
coding tasks through external CLI agents. A worker model has stopped. Decide
what happens next.

Answer with JSON only, no prose:
{"verdict": "ok|retry|rotate_model|rotate_account|fatal|question",
 "confidence": 0.0-1.0,
 "reason": "one short line",
 "action": "what to do, one short line"}

Verdict meanings:
- retry          transient failure (5xx, timeout, 429): try again, capped
- rotate_model   this model's limit only: same account, different model
- rotate_account account/key is out (402 empty wallet, 401/403): another account
- fatal          impossible to continue: no key, broken config, bad task
- question       the worker asked something ai_Pobisk should answer
- ok             nothing wrong, continue

Rules:
- 402 / empty wallet / bad key is rotate_account, NEVER rotate_model
- 429 and 5xx are retry, NEVER fatal
- 404 "model not available" is rotate_model
- Prefer the cheapest recovery that could work."""


class Meta:
    """Asks a configured model what to do. Falls back to rules."""

    def __init__(self, repo_root, cfg, adapters):
        self.repo_root = repo_root
        self.cfg = cfg
        self.adapters = adapters
        self.model = None          # (skill_name, model_id) once found
        self.calls = 0
        self.unavailable_reason = ""

    # ── selection ─────────────────────────────────────────────────────────────
    def select(self, verbose=False):
        """Find the first candidate that answers. Returns True if one did."""
        for cand in self.cfg["meta"]["candidates"]:
            skill, model = cand.get("skill", ""), cand.get("model", "")
            a = self.adapters.get(skill)
            if not a or not a.available():
                continue
            o = a.run("Reply with exactly: ok", model=model,
                      timeout=self.cfg["meta"]["timeout_sec"])
            if o.outcome == OK or (o.raw or "").strip():
                self.model = (skill, model)
                if verbose:
                    print("  meta model: %s / %s (%sms)"
                          % (skill, model, o.elapsed_ms))
                return True
            if verbose:
                print("  meta candidate %s/%s -> %s" % (skill, model, o.outcome))
        self.unavailable_reason = "no meta candidate answered"
        return False

    # ── asking ────────────────────────────────────────────────────────────────
    def decide(self, outcome, context, attempts):
        """Ask the meta model what to do with `outcome`.

        Returns (verdict, confidence, reason). Never raises: on any failure it
        falls back to the rule table.
        """
        allowed = self.cfg["budget"]["max_meta_calls_per_task"]
        if self.calls >= allowed:
            return self._rules(outcome, context, attempts)
        if not self.model:
            return self._rules(outcome, context, attempts)

        prompt = self._prompt(outcome, context, attempts)
        self.calls += 1
        try:
            a = self.adapters[self.model[0]]
            o = a.run(prompt, model=self.model[1],
                      timeout=self.cfg["meta"]["timeout_sec"])
            text = (o.raw or "").strip()
            v, conf, reason = self._parse(text)
            if v:
                return v, conf, reason
            # Not JSON: one re-ask, then rules.
            if self.cfg["meta"]["max_reparse"] > 0:
                self.cfg["meta"]["max_reparse"] -= 1
                o2 = a.run(prompt + "\n\nAnswer with JSON only.",
                           model=self.model[1],
                           timeout=self.cfg["meta"]["timeout_sec"])
                v, conf, reason = self._parse((o2.raw or "").strip())
                if v:
                    return v, conf, reason
        except Exception as e:
            self.unavailable_reason = "meta call failed: %s" % type(e).__name__
        return self._rules(outcome, context, attempts)

    # ── deterministic fallback ────────────────────────────────────────────────
    def _rules(self, outcome, context, attempts):
        """No LLM: map the outcome straight through the rule table.

        Used ONLY when the meta model is unreachable. When it is available the
        model decides — the rule table cannot enumerate the ways a model can
        stall, so treating it as the primary decision-maker is what makes the
        supervisor brittle.
        """
        o = outcome.outcome
        if o == RETRY:
            r = attempts.get("retry", 0)
            cap = self.cfg["budget"]["max_retries_per_step"]
            if r >= cap:
                return ROTATE_MODEL, 1.0, "retry budget spent (%d)" % r
            return RETRY, 1.0, "transient, retry %d/%d" % (r + 1, cap)
        if o == ROTATE_MODEL:
            return ROTATE_MODEL, 1.0, "model limit"
        if o == ROTATE_ACCOUNT:
            return ROTATE_ACCOUNT, 1.0, "account limit"
        if o == LOOP_GUARD:
            return FATAL, 1.0, "loop guard tripped"
        if o == FATAL:
            return FATAL, 1.0, "fatal: %s" % outcome.detail[:80]
        return ROTATE_MODEL, 0.5, "unknown outcome -> rotate model"

    # ── helpers ───────────────────────────────────────────────────────────────
    def _prompt(self, outcome, context, attempts):
        return (
            "%s\n\nA worker model stopped.\n"
            "skill=%s model=%s account=%s\n"
            "outcome=%s\n"
            "detail=%s\n"
            "http=%s elapsed_ms=%s\n"
            "attempts=%s\n"
            "task=%s\nstep=%s\n"
            "Raw output tail:\n%s\n"
            % (SYSTEM_PROMPT, outcome.skill, outcome.model or "-",
               outcome.account or "-", outcome.outcome, outcome.detail,
               outcome.http, outcome.elapsed_ms, attempts,
               context.get("task", "-"), context.get("step", "-"),
               (outcome.raw or "")[-1200:]))

    @staticmethod
    def _parse(text):
        """Pull the JSON object out of whatever the model printed."""
        if not text:
            return None, 0.0, ""
        # Models love to wrap JSON in prose or fences; find the outermost {}.
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None, 0.0, ""
        try:
            d = json.loads(text[start:end + 1])
        except Exception:
            return None, 0.0, ""
        v = str(d.get("verdict", "")).strip().lower()
        valid = (OK, RETRY, ROTATE_MODEL, ROTATE_ACCOUNT, FATAL, "question")
        if v not in valid:
            return None, 0.0, ""
        try:
            conf = float(d.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        return v, conf, str(d.get("reason", ""))[:200]

    def status(self):
        return {
            "model": ("%s/%s" % self.model) if self.model else None,
            "calls": self.calls,
            "available": bool(self.model),
            "unavailable_reason": self.unavailable_reason,
        }
