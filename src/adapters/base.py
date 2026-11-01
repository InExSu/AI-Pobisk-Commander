"""base.py — shared plumbing for every skill adapter.

An adapter's only job: run one skill, and translate whatever that skill
reports into a single Outcome. Adapters never change the skills themselves —
2800 LOC of provider-specific knowledge (404 catalogues, 402 traps, slow
models) stays where it is.

Every adapter inherits `run(prompt, **kw)` and returns an Outcome.
"""

import os
import re
import signal
import subprocess
import threading
import time

HEALTH_STORE = os.path.join(
    os.environ.get("AI_ROTATE_DIR") or os.path.expanduser("~/.ai-rotate"),
    "model-stats.json")

# ── model attribution ────────────────────────────────────────────────────────
# The rotate scripts pick the model themselves and say which one they used:
#
#     pobisk-attr: model=<id> account=<name>
#
# The Python side cannot see inside the script, so without this line the
# journal's `model` column stays empty, the health store never learns which
# model answered, and a rotation has no real starting point. The marker is a
# dedicated one on purpose: the agent's own answer is printed to the same
# stdout and may well contain the words "model=" or "account=" — only the
# marker makes the line unambiguous.
ATTR_MARK = "pobisk-attr:"
_ATTR_RE = re.compile(
    r"pobisk-attr:\s*model=(?P<model>\S*)\s+account=(?P<account>\S*)")

# Per-thread cwd the supervisor sets before hiring a worker. A process-wide
# PB_TARGET_DIR export cannot work under parallel tasks: two threads running
# different tasks would overwrite each other's target and an agent could be
# pointed at the WRONG repository. os.environ has no per-thread scope; a
# thread-local does.
_tls = threading.local()


def set_target_dir(path):
    """Set the cwd run_cmd() uses in THIS thread (None clears it)."""
    _tls.target_dir = path or None


def parse_attribution(text):
    """(model, account) the skill reported it used, or ("", "").

    The last marker line wins: the scripts log chronologically, so the final
    marker names the model that served (or failed) the call.
    """
    model = account = ""
    for line in (text or "").splitlines():
        m = _ATTR_RE.search(line)
        if m:
            model = m.group("model") or ""
            account = m.group("account") or ""
    return model, account


class Adapter:
    """Base class. Subclasses set `skill` and implement `_invoke`."""

    skill = ""
    # Path to the skill's rotate script, relative to the repo root.
    script = ""
    # Does this skill's own rotate script write to the shared health store?
    # All five do. When it does, the adapter must stay silent: two writers for
    # one call double the sample count, and one real failure becomes two
    # strikes on a breaker that opens after three — so the fleet routes around
    # a model that failed only once. The script knows the model; the adapter
    # only guesses, so the script is the single writer.
    script_records_health = False
    # Can this skill touch files on disk? Local CLI agents run a real agent
    # with file tools (read/grep/edit/shell), so yes. A bare chat-completion
    # API has no tools at all — it answers in prose and never opens the repo —
    # so it says no. The supervisor routes file work only to workers that
    # can actually do it.
    has_filesystem = True

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
        # Which model/account actually served. The skill's own report wins over
        # whatever the supervisor pinned: a skill that rotates internally may
        # end up on a different model than the one it was asked to use.
        used_model, used_account = self._attribution(text, model, account)
        o = classify(text, http=_http_from(text), skill=self.skill,
                     model=used_model, account=used_account, elapsed_ms=ms,
                     rc=rc)

        # rc != 0 with no recognised error pattern is a real failure, not a
        # success: the skill gave up but we could not say why. A stall-guard or
        # timeout kill leaves the worker's own "stalled"/"timeout" marker in the
        # output — name it, so the journal shows the model was killed for
        # silence rather than "exited with no recognised error". (Kills are
        # already rotated inside classify() when it sees rc; this branch only
        # catches a kill whose rc shape classify could not have seen.)
        if rc not in (0,) and o.outcome == "ok":
            from outcome import Outcome, RETRY
            reason = "exit %d with no recognised error" % rc
            low = (text or "").lower()
            if "stalled" in low:
                reason = "worker killed by stall guard (silent, rc %d)" % rc
            elif "timeout" in low or "timed out" in low:
                reason = "worker killed by timeout (rc %d)" % rc
            o = Outcome(RETRY, self.skill, used_model, used_account,
                        reason, None, ms, text)
        if not self.script_records_health:
            self._record(o)
        return o

    @staticmethod
    def _attribution(text, model, account):
        """(model, account) that served the call.

        The skill's own report is authoritative; the pinned values are only a
        fallback for skills that do not report (or that were asked to use a
        model the skill then overrode).
        """
        m, a = parse_attribution(text)
        return m or (model or ""), a or (account or "")

    def _record(self, o):
        """Feed the shared health store, if it is available.

        Only for skills whose rotate script does NOT write the store itself
        (see `script_records_health`). When the script does write, this method
        is skipped on purpose: one real call recorded twice doubles the
        uptime sample count and counts a single failure as two strikes.

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
            import importlib.util as _ilu
            _shared = os.path.join(self.repo_root, ".agents", "skills", "_shared")
            if _shared not in sys.path:
                sys.path.insert(0, _shared)
            try:
                import model_stats   # only if a model_stats.py shim exists
            except Exception:
                # The store ships as model-stats.py (hyphen), which a plain
                # `import model_stats` cannot resolve. Load it by path so the
                # adapter-side health recording actually runs instead of
                # silently no-op'ing inside this try/except.
                _spec = _ilu.spec_from_file_location(
                    "model_stats",
                    os.path.join(_shared, "model-stats.py"))
                model_stats = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(model_stats)

            verdict = "fail"
            if o.outcome == "ok":
                verdict = "ok"
            elif o.outcome == "rotate_account" and self._is_credential_failure(o):
                verdict = "auth"
            elif self._is_ratelimit(o):
                # 429/520/529 / "rate limit" / "quota": the provider is
                # throttling, not the model or the account. Park it briefly
                # (model-stats "ratelimit" state) so the fleet routes around
                # a flaky provider instead of hammering it every loop.
                verdict = "limit"

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

    @staticmethod
    def _is_ratelimit(o):
        """True when the provider is throttling (transient, not a model fault).

        Narrow on purpose: 429/520/529 are unambiguous throttle codes, and the
        prose patterns name rate-limit/quota explicitly. Plain 5xx (500/502/503)
        are left as ordinary failures so a genuinely broken model still lands in
        the hard-failure breaker instead of being coddled forever.
        """
        if o.http in (429, 520, 529):
            return True
        d = (o.detail or "").lower()
        for s in ("rate limit", "ratelimit", "quota exceeded",
                  "too many requests", "429", "520", "529"):
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


def run_cmd(argv, timeout=None, env=None, cwd=None):
    """Run a command, return (rc, stdout, stderr). Never raises on non-zero.

    Runs the child in its own process group and kills the *group* on timeout.
    `subprocess.run(timeout=...)` alone kills only the direct child — which for
    these skills is a bash wrapper — leaving the real agent (node cline, an
    opencode TUI) alive and unsupervised, still editing the repository. That
    turns one timed-out step into two agents writing to the same tree.

    `cwd` lets a file-step worker actually run INSIDE the target project, so the
    agent edits the project and not ai_Pobisk's own tree. When `cwd` is not
    given, fall back to the thread-local target (set by the supervisor per
    step — safe under parallel tasks, unlike a shared environment variable),
    then to ``PB_TARGET_DIR``, else the caller's working directory.
    """
    e = os.environ.copy()
    if env:
        e.update(env)
    run_cwd = (cwd or getattr(_tls, "target_dir", None)
               or e.get("PB_TARGET_DIR") or None)
    if run_cwd:
        run_cwd = os.path.expanduser(run_cwd) or None
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=e, start_new_session=True, cwd=run_cwd)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        # Take the whole tree down: the wrapper, the agent, its children.
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            p.kill()
        try:
            out, err = p.communicate(timeout=10)
        except Exception:
            out, err = "", ""
        return 124, out or "", (err or "") + "\ntimed out after %ss" % timeout
