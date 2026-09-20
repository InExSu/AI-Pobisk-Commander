"""classify.py — turn a skill's raw failure signal into one Outcome.

The patterns below are not invented: they are lifted out of the five skills,
where the same knowledge currently lives in five incompatible forms
(cline's `is_model_limit` banner text, opencode's DEAD_PATTERNS /
AUTH_PATTERNS / RETRYABLE_RE, freebuff's HTTP 403 + "no authToken",
nvidia/th's HTTP 404/503 and TimeoutError). One table, five dialects gone.

Order matters — the first matching rule wins, so the specific ones come first:

  402 $0 / "credit insufficient"  -> rotate_account  (NOT the model's fault)
  401/403 / bad key               -> rotate_account  (retrying cannot fix it)
  "no authToken"                  -> rotate_account
  429/503/529/timeout/5xx         -> retry           (NVIDIA 503 is routine)
  daily/per-model limit banner    -> rotate_model
  404 "Model ... not available"   -> rotate_model    (that model only)
  missing key / broken config     -> fatal           (no continuation exists)
  the model asked a question      -> question
"""

import os
import re
import sys

# _shared/ lives next to src/, one level up. Importable as a plain module.
_SHARED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".agents", "skills", "_shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

# ── Rule table: (regex, outcome, why) ─────────────────────────────────────────
# Ordered most-specific first. `why` is what lands in the journal.
RULES = [
    # ── account-level money / credentials ──
    (r"402|balance is at \$0|insufficient balance|credit insufficient|top up|recharge",
     "rotate_account", "wallet empty or daily credit cap spent"),
    (r"no authToken|no api key|no API key|missing api key|api key not configured",
     "rotate_account", "account has no usable credential"),
    (r"\b401\b|Unauthorized|unauthenticated|not authenticated",
     "rotate_account", "credential rejected"),
    (r"\b403\b|Forbidden|invalid api key|Invalid API key|api key is invalid",
     "rotate_account", "credential rejected"),

    # ── per-model quota: only this model is out ──
    (r"Daily free model limit reached|today's free usage limit|"
     r"INFERENCE_CAP_ERROR|Free model promotion ended|"
     r"FreeUsageLimitError|GoUsageLimitError|Free limit reached|FreeTierError",
     "rotate_model", "this model hit its daily cap"),
    (r"\b404\b|is not available|does not exist|unknown model|model not found|"
     r"Model is disabled|Model not found|Function .* not found",
     "rotate_model", "model not deployed / unknown"),

    # ── transient: worth another attempt ──
    (r"\b429\b|\b503\b|\b529\b|\b500\b|\b502\b|\b504\b|"
     r"rate limit|Rate limit|Rate limit exceeded|Quota exceeded|"
     r"Service temporarily overloaded|overloaded|Overloaded|capacity|"
     r"Internal server error|isRetryable|statusCode.*5\d\d",
     "retry", "transient upstream failure"),
    (r"TimeoutError|timed out|timeout|ETIMEDOUT|deadline exceeded",
     "retry", "slow or timed-out call"),

    # ── nothing will fix this without a human ──
    (r"No payment method|payment method here",
     "rotate_account", "account has no billing"),
    (r"CreditsError",
     "rotate_account", "account out of credits"),
]

COMPILED = [(re.compile(p, re.I), o, why) for p, o, why in RULES]

# "No such file or directory" / rc 127 / "command not found": the skill's
# binary or script could not be executed.
_MISSING_BINARY = re.compile(
    r"No such file or directory"
    r"|command not found"
    r"|Permission denied"
    r"|not executable"
    r"|\b127\b",
    re.I)

# A bare chat-completion call (nvidia/tokenharbor) has no file tools. Asked to
# read or edit files, it answers "I don't have access to your filesystem" in
# polite prose and exits 0. Taken at face value that is a SUCCESS — and the
# task silently completes having done nothing. Detect it and report
# rotate_account ("this worker cannot do it, move on").
NO_FILESYSTEM_PATTERNS = re.compile(
    r"I (?:don't|do not|cannot|can't) (?:have|access|directly access)"
    r"[^.]{0,60}(?:file|filesystem|local files|that path|your computer)"
    r"|no access to (?:the |your )?file"
    r"|does not contain that path"
    r"|file system available to me"
    r"|не имею доступа к файловой систем"
    r"|нет доступа к файл"
    r"|(?:paste|share) the contents of",
    re.I)

# ── The model stopped to WAIT, not to fail ────────────────────────────────────
# Agents frequently halt on a confirmation prompt — "Press Enter to continue",
# "Continue? [y/N]", " press any key", "Proceed? (yes/no)" — and then block on
# stdin that nobody writes to. In a supervisor that is the worst outcome: the
# run neither fails nor progresses, it just hangs until the timeout, and the
# failure that finally surfaces looks like a random model error.
#
# These are NOT questions (the answer is not information, it is "go on") and
# NOT errors. They get their own outcome so the supervisor can answer for the
# user and tell the model to keep working.
WAITING_PATTERNS = re.compile(
    r"press (?:enter|any key|return|space)"
    r"|press \[enter\]"
    r"|\(y/n\)|\[y/n\]|\[Y/n\]|\(yes/no\)|\[yes/no\]"
    r"|continue\?\s*(?:\[|y/n)"
    r"|proceed\?\s*(?:\[|y/n)"
    r"|confirm?\s*(?:\[|y/n)"
    r"|do you want to (?:continue|proceed)"
    r"|нажмите (?:enter|клавишу|любую клавишу|ввод)"
    r"|продолжить\?\s*(?:\[|да/нет)"
    r"|для продолжения нажмите"
    r"|hit (?:enter|return)"
    r"|\[waiting for (?:input|confirmation|user)\]"
    r"|awaiting (?:your )?(?:input|confirmation|approval)"
    r"|waiting for (?:your )?(?:input|confirmation|approval|response)",
    re.I)

# Signals that the model stopped to ask something rather than to fail.
QUESTION_PATTERNS = re.compile(
    r"^\s*(which|what|should (i|we)|do you (want|prefer)|"
    r"can you (clarify|confirm|choose)|please (clarify|confirm|choose|specify))"
    r"\b|\?\s*$",
    re.I | re.M)


# Skills that are a bare chat-completion call with no agent tools. Keep this
# next to the pattern: adding a skill here is what arms the check for it.
NO_FILESYSTEM_SKILLS = ("nvidia", "tokenharbor")


def classify(text, http=None, skill="", model="", account="", elapsed_ms=0,
             no_fs_skills=NO_FILESYSTEM_SKILLS):
    """Map raw output + HTTP code to an Outcome.

    HTTP code is consulted when present: it is more reliable than prose, which
    is why it is checked before the text rules.

    `no_fs_skills` lists skills that are bare chat completions with no file
    tools. For those, "I cannot access your files" is not an answer — it is a
    capability gap, reported as rotate_account so the supervisor moves to a
    skill that can actually do the work.
    """
    from outcome import (
        Outcome, OK, FATAL, RETRY, ROTATE_MODEL, ROTATE_ACCOUNT,
        QUESTION, WAITING)

    text = text or ""
    detail = _brief(text, http)

    # Checked FIRST, before any error rule: a confirmation prompt is not a
    # failure, and error patterns (429, "continue?") can appear in the same
    # banner. Treating it as an error would rotate the model away from work it
    # was perfectly able to finish.
    if WAITING_PATTERNS.search(text):
        return Outcome(WAITING, skill, model, account,
                       "blocked on a confirmation prompt",
                       http, elapsed_ms, text)

    # A missing binary / broken invocation. This never fixes itself by
    # retrying, and it fails in milliseconds — without this check the
    # supervisor spins thousands of iterations per minute.
    if _MISSING_BINARY.search(text):
        return Outcome(ROTATE_ACCOUNT, skill, model, account,
                       "skill binary missing or not executable",
                       http, elapsed_ms, text)

    # HTTP code first — prose lies, status codes rarely do.
    if http is not None:
        try:
            code = int(http)
        except (TypeError, ValueError):
            code = None
        if code == 402:
            return Outcome(ROTATE_ACCOUNT, skill, model, account,
                           "wallet empty (402)", code, elapsed_ms, text)
        if code in (401, 403):
            return Outcome(ROTATE_ACCOUNT, skill, model, account,
                           "credential rejected", code, elapsed_ms, text)
        if code == 404:
            return Outcome(ROTATE_MODEL, skill, model, account,
                           "model not found", code, elapsed_ms, text)
        if code == 429 or code == 529 or code >= 500:
            return Outcome(RETRY, skill, model, account,
                           "transient HTTP %d" % code, code, elapsed_ms, text)

    for rx, outcome, why in COMPILED:
        if rx.search(text):
            return Outcome(outcome, skill, model, account,
                           "%s (%s)" % (why, _match(rx, text)),
                           http, elapsed_ms, text)

    # Capability gap, checked before the benign path: a "no filesystem" reply
    # is prose with no error string, so it would otherwise be scored `ok`.
    if skill in no_fs_skills and NO_FILESYSTEM_PATTERNS.search(text or ""):
        return Outcome(ROTATE_ACCOUNT, skill, model, account,
                       "no file access: bare completion cannot do file work",
                       http, elapsed_ms, text)

    # No error signal at all: this is a normal answer, not a failure. Marking it
    # fatal here would kill every healthy run the moment a skill printed
    # something the rule table did not recognise.
    stripped = text.strip()
    if not stripped:
        return Outcome(RETRY, skill, model, account,
                       "empty response", http, elapsed_ms, text)

    if QUESTION_PATTERNS.search(stripped) and len(stripped) < 600:
        return Outcome(QUESTION, skill, model, account,
                       _brief(stripped), http, elapsed_ms, text)

    return Outcome(OK, skill, model, account,
                   _brief(stripped, http), http, elapsed_ms, text)


def _match(rx, text):
    """The matched snippet, for the journal — short enough to read."""
    m = rx.search(text)
    if not m:
        return ""
    s = m.group(0).strip()
    return s[:80]


def _brief(text, http=None):
    """One-line summary: the first meaningful line, not the whole dump."""
    if not text:
        return "HTTP %s" % http if http else "no output"
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("[", "Warning:", "warning:")):
            return "%s%s" % (line[:160], " (HTTP %s)" % http if http else "")
    return "%s%s" % (text[:160], " (HTTP %s)" % http if http else "")
