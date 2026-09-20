"""base.py — shared plumbing for every skill adapter.

An adapter's only job: run one skill, and translate whatever that skill
reports into a single Outcome. Adapters never change the skills themselves —
2800 LOC of provider-specific knowledge (404 catalogues, 402 traps, slow
models) stays where it is.

Every adapter inherits `run(prompt, **kw)` and returns an Outcome.
"""

import os
import subprocess
import time

HEALTH_STORE = os.path.join(
    os.environ.get("AI_ROTATE_DIR") or os.path.expanduser("~/.ai-rotate"),
    "model-stats.json")


class Adapter:
    """Base class. Subclasses set `skill` and implement `_invoke`."""

    skill = ""
    # Path to the skill's rotate script, relative to the repo root.
    script = ""

    def __init__(self, repo_root):
        self.repo_root = repo_root
        self.script_path = os.path.join(repo_root, self.script) if self.script else ""

    # ── the one method subclasses implement ───────────────────────────────────
    def _invoke(self, prompt, model=None, account=None, timeout=None):
        """Run the skill. Return (returncode, stdout, stderr)."""
        raise NotImplementedError

    # ── everything else is shared ─────────────────────────────────────────────
    def run(self, prompt, model=None, account=None, timeout=None):
        """Invoke the skill and classify whatever comes back."""
        from classify import classify

        t0 = time.time()
        try:
            rc, out, err = self._invoke(prompt, model=model,
                                        account=account, timeout=timeout)
        except subprocess.TimeoutExpired:
            rc, out, err = -1, "", "TimeoutExpired"
        except FileNotFoundError as e:
            from outcome import fatal
            return fatal(self.skill, model or "", account or "",
                         "skill script missing: %s" % e)
        except Exception as e:  # never crash the supervisor on a skill bug
            from outcome import fatal
            return fatal(self.skill, model or "", account or "",
                         "adapter error: %s: %s" % (type(e).__name__, e))

        ms = int((time.time() - t0) * 1000)
        text = (out or "") + "\n" + (err or "")
        o = classify(text, http=_http_from(text), skill=self.skill,
                     model=model or "", account=account or "", elapsed_ms=ms)

        # rc != 0 with no recognised error pattern is a real failure, not a
        # success: the skill gave up but we could not say why.
        if rc not in (0,) and o.outcome == "ok":
            from outcome import Outcome, RETRY
            o = Outcome(RETRY, self.skill, model or "", account or "",
                        "exit %d with no recognised error" % rc,
                        None, ms, text)
        self._record(o)
        return o

    def _record(self, o):
        """Feed the shared health store, if it is available.

        CRITICAL: `auth` parks a model PERMANENTLY. It must mean only one
        thing — this model's credential is broken. It must never be used for
        "the account ran out", "the call timed out" or "something odd
        happened": that is how 19 of cline's 20 healthy models ended up
        parked during experiments, collapsing rotation to a single model
        without anyone noticing. When in doubt, record `fail` (transient,
        recovers on its own) instead.
        """
        try:
            import sys
            _shared = os.path.join(self.repo_root, ".agents", "skills", "_shared")
            if _shared not in sys.path:
                sys.path.insert(0, _shared)
            import model_stats

            verdict = "fail"
            if o.outcome == "ok":
                verdict = "ok"
            elif o.outcome == "rotate_account" and self._is_credential_failure(o):
                verdict = "auth"

            # Never park a model we could not even name — that writes junk
            # entries like skill:"unknown" into the store.
            model = o.model or ""
            if not model:
                return
            model_stats.cmd_record([self.skill, model, verdict,
                                    str(o.elapsed_ms), o.detail])
        except Exception:
            pass  # health store is best-effort; never block the supervisor

    @staticmethod
    def _is_credential_failure(o):
        """True only when the CREDENTIAL is bad, not the account's balance."""
        d = (o.detail or "").lower()
        if o.http in (401, 403):
            return True
        for s in ("credential rejected", "auth error", "invalid api key",
                  "unauthorized", "unauthenticated", "no authtoken",
                  "no api key", "missing api key", "not authenticated"):
            if s in d:
                return True
        return False

    def available(self):
        """Is this skill usable right now? (script exists, key present.)"""
        return bool(self.script_path) and os.path.exists(self.script_path)

    def __repr__(self):
        return "<%s skill=%s>" % (type(self).__name__, self.skill)


def _http_from(text):
    """Pull an HTTP status out of prose, if one is printed."""
    import re
    m = re.search(r"HTTP (\d{3})", text or "")
    if m:
        return int(m.group(1))
    m = re.search(r"\b([45]\d\d)\b", text or "")
    return int(m.group(1)) if m else None


def run_cmd(argv, timeout=None, env=None):
    """Run a command, return (rc, stdout, stderr). Never raises on non-zero."""
    e = os.environ.copy()
    if env:
        e.update(env)
    p = subprocess.run(argv, capture_output=True, text=True,
                       timeout=timeout, env=e)
    return p.returncode, p.stdout or "", p.stderr or ""
