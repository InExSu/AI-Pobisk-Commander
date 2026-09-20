"""outcome.py — the one vocabulary every skill speaks.

Each rotate skill currently reports failure in its own dialect: cline matches
banner text, opencode returns rc 0/1/2 with three regex sets, freebuff exits
1/4/5, nvidia/th exit 3 plus an HTTP code. A supervisor cannot reason about
"is this fatal?" across five dialects, so every adapter translates its skill's
signal into exactly one of these:

    ok              task step done
    retry           transient: 5xx, timeout, 429 — try again, capped
    rotate_model    this model's limit only — same account, different model
    rotate_account  account/key limit (402, 401/403, quota) — different account
    fatal           never continuable: no key, broken config, bad task
    question        the model asked something — needs a decision
    loop_guard      spinning: retry/rotation budget exhausted

Nothing here imports a skill; skills import this. Keep it dependency-free.
"""

OK = "ok"
RETRY = "retry"
ROTATE_MODEL = "rotate_model"
ROTATE_ACCOUNT = "rotate_account"
FATAL = "fatal"
QUESTION = "question"
WAITING = "waiting"
LOOP_GUARD = "loop_guard"

ALL = (OK, RETRY, ROTATE_MODEL, ROTATE_ACCOUNT, FATAL, QUESTION, WAITING,
       LOOP_GUARD)

# What each outcome means for the supervisor's next move.
RECOVERABLE = (RETRY, ROTATE_MODEL, ROTATE_ACCOUNT)
STOPS_TASK = (FATAL, LOOP_GUARD)
NEEDS_DECISION = (QUESTION,)
# The model is blocked on a confirmation prompt. ai_Pobisk answers for the
# user and tells it to continue — no owner involvement.
NEEDS_NUDGE = (WAITING,)


class Outcome:
    """A single, machine-readable result of one skill invocation."""

    __slots__ = ("outcome", "skill", "model", "account", "detail",
                 "http", "elapsed_ms", "raw")

    def __init__(self, outcome, skill="", model="", account="", detail="",
                 http=None, elapsed_ms=0, raw=""):
        if outcome not in ALL:
            raise ValueError("unknown outcome %r" % (outcome,))
        self.outcome = outcome
        self.skill = skill
        self.model = model
        self.account = account
        self.detail = (detail or "")[:2000]
        self.http = http
        self.elapsed_ms = elapsed_ms
        self.raw = (raw or "")[:4000]

    # ── predicates the supervisor asks ────────────────────────────────────────
    @property
    def ok(self):
        return self.outcome == OK

    @property
    def recoverable(self):
        return self.outcome in RECOVERABLE

    @property
    def stops_task(self):
        return self.outcome in STOPS_TASK

    @property
    def needs_decision(self):
        return self.outcome in NEEDS_DECISION

    @property
    def needs_nudge(self):
        return self.outcome in NEEDS_NUDGE

    def to_dict(self):
        return {
            "outcome": self.outcome,
            "skill": self.skill,
            "model": self.model,
            "account": self.account,
            "detail": self.detail,
            "http": self.http,
            "elapsed_ms": self.elapsed_ms,
            "raw": self.raw,
        }

    @staticmethod
    def from_dict(d):
        return Outcome(
            d.get("outcome", FATAL), d.get("skill", ""), d.get("model", ""),
            d.get("account", ""), d.get("detail", ""), d.get("http"),
            d.get("elapsed_ms", 0), d.get("raw", ""))

    def __repr__(self):
        bits = [self.outcome]
        if self.skill:
            bits.append("skill=%s" % self.skill)
        if self.model:
            bits.append("model=%s" % self.model)
        if self.detail:
            bits.append("detail=%r" % self.detail[:60])
        return "<Outcome %s>" % " ".join(bits)


def ok(skill="", model="", account="", detail="", elapsed_ms=0, raw=""):
    return Outcome(OK, skill, model, account, detail, None, elapsed_ms, raw)


def retry(skill="", model="", account="", detail="", http=None, elapsed_ms=0, raw=""):
    return Outcome(RETRY, skill, model, account, detail, http, elapsed_ms, raw)


def rotate_model(skill="", model="", account="", detail="", http=None,
                 elapsed_ms=0, raw=""):
    return Outcome(ROTATE_MODEL, skill, model, account, detail, http, elapsed_ms, raw)


def rotate_account(skill="", model="", account="", detail="", http=None,
                   elapsed_ms=0, raw=""):
    return Outcome(ROTATE_ACCOUNT, skill, model, account, detail, http,
                   elapsed_ms, raw)


def fatal(skill="", model="", account="", detail="", http=None, elapsed_ms=0, raw=""):
    return Outcome(FATAL, skill, model, account, detail, http, elapsed_ms, raw)


def question(skill="", model="", account="", detail="", elapsed_ms=0, raw=""):
    return Outcome(QUESTION, skill, model, account, detail, None, elapsed_ms, raw)


def loop_guard(skill="", model="", account="", detail="", elapsed_ms=0, raw=""):
    return Outcome(LOOP_GUARD, skill, model, account, detail, None, elapsed_ms, raw)


def waiting(skill="", model="", account="", detail="", elapsed_ms=0, raw=""):
    return Outcome(WAITING, skill, model, account, detail, None, elapsed_ms, raw)
