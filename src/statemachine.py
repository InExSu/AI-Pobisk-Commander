"""statemachine.py — the control machine of ai_Pobisk and the DRAKON silhouette.

The README asks for one thing before anything else: a control state machine,
and the DRAKON silhouette drawn *from* it. The reason is not decoration. The
supervisor used to be a ladder of `if` statements in `supervisor.py`; every
recovery rule lived in one function and nothing could tell you whether the
rules still agreed with each other. A machine makes the loop inspectable:

    STATES       every place the supervisor can be
    TRANSITIONS  every legal move, with the event that causes it
    decide()     the rule table, executed
    silhouette() the same table, drawn

`validate()` proves the graph has no dead ends: every state is reachable from
`dispatch`, every non-terminal state has an exit, and every state can still
reach a terminal. `test_statemachine.py` asserts the drawing only contains
edges the machine actually has, so the picture cannot drift from the code.

Why DRAKON: it is a flow language built for exactly this shape — a vertical
trunk of actions with decision branches hanging to the right and returning by
address. The silhouette is the trunk; the branches are the recovery paths.

Zero dependencies: stdlib only, and the SVG is written by hand.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, os.path.join(_ROOT, ".agents", "skills", "_shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

from outcome import (OK, RETRY, ROTATE_MODEL, ROTATE_ACCOUNT,  # noqa: E402
                     FATAL, QUESTION, WAITING, LOOP_GUARD)

# ── states ────────────────────────────────────────────────────────────────────
DISPATCH = "dispatch"          # pick a worker + model, call the adapter
EVALUATE = "evaluate"          # read the Outcome, apply the rules
NUDGE = "nudge"                # answer a confirmation prompt, carry on
QUESTION_ST = "question"       # competence check
META = "meta"                  # the rule table has no answer: ask the model
RETRY_ST = "retry"             # same worker, same model, again
ROTATE_MODEL_ST = "rotate_model"
ROTATE_ACCOUNT_ST = "rotate_account"

# terminals
OK_ST = "ok"
FATAL_ST = "fatal"
LOOP_ST = "loop_guard"
EXHAUSTED_ST = "exhausted"
NEEDS_OWNER_ST = "needs_owner"

TERMINAL = {
    OK_ST: "шаг выполнен",
    FATAL_ST: "продолжить нельзя — доложить хозяину",
    LOOP_ST: "крутится — остановить",
    EXHAUSTED_ST: "бюджеты/воркеры кончились",
    NEEDS_OWNER_ST: "вопрос вне компетенции — ждать хозяина",
}

# What `run_step` returns to `run_task`. The two terminal names that are not
# returned verbatim are "ok" (step succeeded) and the rest, which are already
# the words `run_task` reports.
RETURNS = {
    OK_ST: "ok",
    FATAL_ST: "fatal",
    LOOP_ST: "loop_guard",
    EXHAUSTED_ST: "exhausted",
    NEEDS_OWNER_ST: "needs_owner",
}

# ── transitions ───────────────────────────────────────────────────────────────
# (from, event, to, note). `event` is the thing that happened, not a verb the
# caller invents: outcomes, invariant verdicts, budget verdicts, meta verdicts.
TRANSITIONS = [
    # entry
    (DISPATCH, "call", EVALUATE, "адаптер вернул Outcome"),
    (EVALUATE, "outcome=ok", OK_ST, "шаг сделан"),
    # hard invariants — checked before anything else may vote
    (EVALUATE, "invariant=stop_loop", LOOP_ST, "уже крутится"),
    (EVALUATE, "invariant=rotate_model", ROTATE_MODEL_ST, "лимит модели"),
    (EVALUATE, "invariant=rotate_account", ROTATE_ACCOUNT_ST,
     "ключ/кошелёк/нет способности"),
    (EVALUATE, "invariant=stop_budget", EXHAUSTED_ST, "бюджет исчерпан"),
    # confirmations: nobody at the keyboard, answer "continue"
    (EVALUATE, "outcome=waiting", NUDGE, "модель ждёт ввода"),
    (NUDGE, "nudges=ok", DISPATCH, "подтолкнули и продолжили"),
    (NUDGE, "nudges=exceeded", LOOP_ST, "петля подтверждений"),
    # questions
    (EVALUATE, "outcome=question", QUESTION_ST, "модель спросила"),
    (QUESTION_ST, "answer=local", DISPATCH, "ответ есть в контексте"),
    (QUESTION_ST, "answer=owner", NEEDS_OWNER_ST, "вне компетенции"),
    # already spinning
    (EVALUATE, "outcome=loop_guard", LOOP_ST, "loop guard сработал"),
    # the long tail: the model decides
    (EVALUATE, "rules=no_answer", META, "таблица правил не дала ответа"),
    (META, "verdict=ok", OK_ST, "модель: всё в порядке"),
    (META, "verdict=retry", RETRY_ST, "модель: повторить"),
    (META, "verdict=rotate_model", ROTATE_MODEL_ST, "модель: другая модель"),
    (META, "verdict=rotate_account", ROTATE_ACCOUNT_ST, "модель: другой аккаунт"),
    (META, "verdict=fatal", FATAL_ST, "модель: фатально"),
    (META, "verdict=question", QUESTION_ST, "модель: это вопрос"),
    (META, "verdict=unknown", FATAL_ST, "непонятый вердикт — не гадаем"),
    # loops back to the trunk
    (RETRY_ST, "budget=retry_left", DISPATCH, "повторить, лимит N"),
    (RETRY_ST, "budget=retry_spent", ROTATE_MODEL_ST, "ретраи кончились"),
    (ROTATE_MODEL_ST, "rotate", DISPATCH, "следующая модель"),
    (ROTATE_ACCOUNT_ST, "rotate", DISPATCH, "следующий воркер/аккаунт"),
    # the backstops that make every other rule safe to be wrong about
    (DISPATCH, "budget=iterations", LOOP_ST, "жёсткий предел итераций"),
    (DISPATCH, "budget=no_workers", EXHAUSTED_ST, "воркеры кончились"),
    # …but "no workers" is checked once more after looking for hands: a model
    # in a 15-minute cooldown is not a dead fleet, and the supervisor is
    # allowed to lift the cooldown and walk the list again.
    (DISPATCH, "hands=revived", DISPATCH, "сняли кулдауны, идём по кругу"),
    (DISPATCH, "hands=none", EXHAUSTED_ST, "рук действительно больше нет"),
]

# Every state that is not a terminal must have an exit.
STATES = sorted({t[0] for t in TRANSITIONS} | {t[2] for t in TRANSITIONS})

INITIAL = DISPATCH


class Decision:
    """What the machine says to do next, and why."""

    __slots__ = ("state", "reason", "event", "source")

    def __init__(self, state, reason="", event="", source="rules"):
        self.state = state
        self.reason = reason
        self.event = event
        self.source = source        # "rules" | "invariant" | "meta" | "budget"

    @property
    def terminal(self):
        return self.state in TERMINAL

    @property
    def result(self):
        """The string `run_task` reports, or None if the loop continues."""
        return RETURNS.get(self.state)

    def __repr__(self):
        return "<Decision %s (%s)>" % (self.state, self.reason or self.event)


# ── the rule table, executed ──────────────────────────────────────────────────
def guard(counters, budgets):
    """Checks made *before* a worker is called. Returns a Decision or None."""
    if counters["iters"] > budgets["max_iters"]:
        return Decision(LOOP_ST, "iteration cap (%d)" % budgets["max_iters"],
                        "budget=iterations", "budget")
    if counters["worker_idx"] >= budgets["n_workers"]:
        return Decision(EXHAUSTED_ST, "no workers left",
                        "budget=no_workers", "budget")
    return None


def invariant(o, counters, budgets):
    """Hard rules the meta model may NOT override.

    Returns (directive, reason) or None. Kept separate from `decide` because
    these are the invariants the README insists stay in code: budgets,
    credentials, capability gaps, confirmations, and an already-spinning loop.
    """
    if o.outcome == LOOP_GUARD:
        return "stop_loop", "already spinning"

    # Nobody at the keyboard: handled by the nudge branch, not a question.
    if o.outcome == WAITING:
        return None

    if o.outcome == RETRY and counters.get("retry", 0) >= budgets["max_retries"]:
        return "rotate_model", "retry budget spent (%d)" % counters.get("retry", 0)

    if o.outcome == ROTATE_ACCOUNT:
        return "rotate_account", "account/key failure"

    # A bare completion saying "I cannot access your files" is a capability
    # gap, not bad luck — no amount of asking changes it.
    if "no file access" in (o.detail or "").lower():
        return "rotate_account", "capability gap"

    return None


_INVARIANT_STATE = {
    "stop_loop": LOOP_ST,
    "rotate_model": ROTATE_MODEL_ST,
    "rotate_account": ROTATE_ACCOUNT_ST,
    "stop_budget": EXHAUSTED_ST,
}


def decide(o, counters, budgets):
    """The rule table. Returns a Decision.

    Order matters and is the point: invariants first, then the cases the code
    owns (confirmations, questions), then the meta model. Anything the rules
    settle never costs a call.
    """
    if o.outcome == OK:
        return Decision(OK_ST, "step done", "outcome=ok")

    inv = invariant(o, counters, budgets)
    if inv:
        directive, reason = inv
        return Decision(_INVARIANT_STATE[directive], reason,
                        "invariant=%s" % directive, "invariant")

    if o.outcome == WAITING:
        if counters.get("nudges", 0) + 1 > budgets["max_nudges"]:
            return Decision(LOOP_ST, "stuck on prompts (%d nudges)"
                            % (counters.get("nudges", 0) + 1),
                            "nudges=exceeded", "invariant")
        return Decision(NUDGE, "model waits for input", "outcome=waiting")

    if o.outcome == QUESTION:
        return Decision(QUESTION_ST, "model asked", "outcome=question")

    if o.outcome == LOOP_GUARD:          # belt and braces; invariant caught it
        return Decision(LOOP_ST, "loop guard", "outcome=loop_guard")

    return Decision(META, "rules have no answer", "rules=no_answer")


def resolve_meta(verdict, confidence, counters, budgets):
    """Turn the meta model's answer into a state.

    The code still owns the budget: a model that says "retry" forever would
    otherwise run forever. Below the confidence floor a `fatal` is downgraded
    to "try somewhere else" — an unsure model does not get to stop the run.
    """
    if verdict == FATAL:
        if confidence < budgets.get("fatal_floor", 0.7):
            return Decision(ROTATE_ACCOUNT_ST, "fatal, low confidence (%.2f)"
                            % confidence, "verdict=fatal", "meta")
        return Decision(FATAL_ST, "fatal", "verdict=fatal", "meta")

    if verdict == RETRY:
        if counters.get("retry", 0) + 1 <= budgets["max_retries"]:
            return Decision(RETRY_ST, "transient", "verdict=retry", "meta")
        return Decision(ROTATE_MODEL_ST, "retry budget spent",
                        "verdict=retry", "meta")

    if verdict == ROTATE_MODEL:
        return Decision(ROTATE_MODEL_ST, "model limit", "verdict=rotate_model",
                        "meta")
    if verdict == ROTATE_ACCOUNT:
        return Decision(ROTATE_ACCOUNT_ST, "account limit",
                        "verdict=rotate_account", "meta")
    if verdict == OK:
        return Decision(OK_ST, "meta: fine", "verdict=ok", "meta")
    if verdict == QUESTION:
        return Decision(QUESTION_ST, "meta: question", "verdict=question", "meta")

    return Decision(FATAL_ST, "unknown verdict %r" % (verdict,),
                    "verdict=unknown", "meta")


# ── graph properties ──────────────────────────────────────────────────────────
def edges():
    return {(f, t) for f, _, t, _ in TRANSITIONS}


def _out(state):
    return [t for f, _, t, _ in TRANSITIONS if f == state]


def reachable(start=INITIAL):
    seen, stack = {start}, [start]
    while stack:
        for nxt in _out(stack.pop()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _can_reach_terminal(state):
    """Every state must have a way out to a terminal — no traps."""
    seen, stack = {state}, [state]
    while stack:
        cur = stack.pop()
        if cur in TERMINAL:
            return True
        for nxt in _out(cur):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def validate():
    """Structural problems with the machine. Empty list means sound."""
    problems = []
    known = set(STATES)
    for f, ev, t, _ in TRANSITIONS:
        if f not in known or t not in known:
            problems.append("unknown state in %s --%s--> %s" % (f, ev, t))

    unreachable = known - reachable()
    if unreachable:
        problems.append("unreachable: %s" % ", ".join(sorted(unreachable)))

    for s in known:
        if s not in TERMINAL and not _out(s):
            problems.append("dead end: %s has no exit" % s)
        if not _can_reach_terminal(s):
            problems.append("trap: %s cannot reach a terminal" % s)

    # A terminal that also has exits would be a contradiction.
    for s in TERMINAL:
        if _out(s):
            problems.append("terminal %s has outgoing edges" % s)
    return problems


# ── the silhouette ────────────────────────────────────────────────────────────
# The drawing, as data. Every branch's "yes" target must exist in TRANSITIONS —
# `test_statemachine.py` enforces it, so the picture is the machine.
#
# node kinds:  action | branch | terminal | address
TRUNK = [
    ("action", "dispatch: выбрать воркера и модель"),
    ("action", "вызвать адаптер → Outcome"),
    ("branch", "итераций > предела?", [
        ("terminal", "loop_guard")]),
    ("branch", "воркеры остались?", [
        ("action", "искать руки: снять кулдауны, сбросить tried"),
        ("branch", "нашли?", [
            ("address", "dispatch")]),
        ("terminal", "exhausted")]),
    ("branch", "outcome = ok?", [
        ("terminal", "ok")]),
    ("branch", "инвариант сработал?", [
        ("action", "лимит модели → другая модель"),
        ("action", "ключ/кошелёк/нет способности → другой аккаунт"),
        ("action", "уже крутится → стоп"),
        ("terminal", "loop_guard / exhausted")]),
    ("branch", "outcome = waiting?", [
        ("action", "подтолкнуть: «продолжай без подтверждений»"),
        ("branch", "nudges > предела?", [
            ("terminal", "loop_guard")]),
        ("address", "dispatch")]),
    ("branch", "outcome = question?", [
        ("action", "проверить компетенцию"),
        ("branch", "ответ есть в контексте?", [
            ("address", "dispatch")]),
        ("terminal", "needs_owner")]),
    ("branch", "outcome = loop_guard?", [
        ("terminal", "loop_guard")]),
    ("action", "спросить мета-модель (нет её — таблица правил)"),
    ("branch", "verdict = fatal и conf ≥ порога?", [
        ("terminal", "fatal")]),
    ("branch", "verdict = retry?", [
        ("branch", "ретраи остались?", [
            ("address", "dispatch")]),
        ("address", "rotate_model")]),
    ("branch", "verdict = rotate_model?", [
        ("address", "rotate_model")]),
    ("branch", "verdict = rotate_account?", [
        ("address", "rotate_account")]),
    ("terminal", "fatal: непонятый вердикт"),
]

# Addresses are the loop edges: they land back on the trunk.
ADDRESSES = {
    "dispatch": "dispatch",
    "rotate_model": "rotate_model",
    "rotate_account": "rotate_account",
}

# Branch yes-target -> the transition event it stands for. Used to prove the
# drawing is a subset of the machine.
_BRANCH_EVENT = {
    "итераций > предела?": ("dispatch", "budget=iterations", "loop_guard"),
    "воркеры остались?": ("dispatch", "budget=no_workers", "exhausted"),
    "нашли?": ("dispatch", "hands=revived", "dispatch"),
    "outcome = ok?": ("evaluate", "outcome=ok", "ok"),
    "инвариант сработал?": ("evaluate", "invariant=rotate_model", "rotate_model"),
    "outcome = waiting?": ("evaluate", "outcome=waiting", "nudge"),
    "outcome = question?": ("evaluate", "outcome=question", "question"),
    "outcome = loop_guard?": ("evaluate", "outcome=loop_guard", "loop_guard"),
    "verdict = fatal и conf ≥ порога?": ("meta", "verdict=fatal", "fatal"),
    "verdict = retry?": ("meta", "verdict=retry", "retry"),
    "verdict = rotate_model?": ("meta", "verdict=rotate_model", "rotate_model"),
    "verdict = rotate_account?": ("meta", "verdict=rotate_account",
                                  "rotate_account"),
    "nudges > предела?": ("nudge", "nudges=exceeded", "loop_guard"),
    "ответ есть в контексте?": ("question", "answer=local", "dispatch"),
    "ретраи остались?": ("retry", "budget=retry_left", "dispatch"),
}


def silhouette_problems():
    """Edges drawn that the machine does not have. Empty list = honest picture."""
    have = {(f, ev, t) for f, ev, t, _ in TRANSITIONS}
    bad = []
    for label, triple in _BRANCH_EVENT.items():
        if triple not in have:
            bad.append("%s -> %s" % (label, triple))
    for name in ADDRESSES:
        if name not in {t for _, _, t, _ in TRANSITIONS}:
            bad.append("address %s is not a state" % name)
    return bad


def ascii_art():
    """The silhouette in text: trunk vertical, branches to the right."""
    out = []

    def icon(sub):
        if sub[0] == "terminal":
            return "■ %s" % sub[1]
        if sub[0] == "address":
            return "○ → %s" % sub[1]
        if sub[0] == "action":
            return "▭ %s" % sub[1]
        return "◇ %s" % sub[1]

    for node in TRUNK:
        kind = node[0]
        if kind == "action":
            out.append("│  ▭ %s" % node[1])
        elif kind == "terminal":
            out.append("│  ■ %s" % node[1])
        elif kind == "branch":
            subs = node[2]
            out.append("│  ◇ %s ── да ──► %s" % (node[1], icon(subs[0])))
            pad = "│  " + " " * (len(node[1]) + 3)
            for sub in subs[1:]:
                out.append("%s├─► %s" % (pad, icon(sub)))
                if sub[0] == "branch":
                    for sub2 in sub[2]:
                        out.append("%s│     └─► %s" % (pad, icon(sub2)))
            out.append("│  ▼ нет")
    return "\n".join(out)


# ── SVG ───────────────────────────────────────────────────────────────────────
_W = 980          # canvas width
_TRUNK_X = 40     # left edge of the trunk
_BOX_W = 330      # trunk box width
_BRANCH_X = 470   # left edge of the right column
_BRANCH_W = 460

_FONT = "font-family='-apple-system, Segoe UI, Roboto, sans-serif'"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _wrap(text, width):
    """Crude wrap by character count — labels are short, this is enough."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _text(x, y, s, size=13, weight="normal", anchor="start", fill="#e6e6e6"):
    return ("<text x='%d' y='%d' font-size='%d' font-weight='%s' "
            "text-anchor='%s' fill='%s' %s>%s</text>"
            % (x, y, size, weight, anchor, fill, _FONT, _esc(s)))


def _box_height(lines, line_h=17, pad=14):
    return len(lines) * line_h + pad * 2


def _action_box(x, y, w, text, fill="#1f2733", stroke="#4a5b70"):
    lines = _wrap(text, max(18, int(w / 7.2)))
    h = _box_height(lines)
    parts = ["<rect x='%d' y='%d' width='%d' height='%d' rx='6' "
             "fill='%s' stroke='%s' stroke-width='1.5'/>"
             % (x, y, w, h, fill, stroke)]
    for i, line in enumerate(lines):
        parts.append(_text(x + 12, y + 14 + i * 17 + 5, line))
    return "".join(parts), h


def _hexagon(x, y, w, h):
    """DRAKON question icon: a flat hexagon, not a diamond."""
    pts = "%d,%d %d,%d %d,%d %d,%d %d,%d %d,%d" % (
        x, y, x + w - 22, y, x + w, y + h // 2, x + w - 22, y + h,
        x, y + h, x + 22, y + h // 2)
    return ("<polygon points='%s' fill='#202a38' stroke='#6f86a3' "
            "stroke-width='1.5'/>" % pts)


def _branch_box(x, y, w, text, fill="#202a38", stroke="#6f86a3"):
    lines = _wrap(text, max(18, int((w - 50) / 7.2)))
    h = max(_box_height(lines), 40)
    parts = [_hexagon(x, y, w, h)]
    for i, line in enumerate(lines):
        parts.append(_text(x + 26, y + h / 2 - (len(lines) - 1) * 8.5 + i * 17 + 4,
                           line))
    return "".join(parts), h


def _terminal_box(x, y, w, text, kind="ok"):
    colors = {"ok": ("#123524", "#3f8f63"), "stop": ("#3a1a1a", "#b4534f")}
    fill, stroke = colors.get(kind, ("#2a2233", "#8a6fb0"))
    lines = _wrap(text, max(18, int((w - 24) / 7.2)))
    h = _box_height(lines)
    parts = ["<rect x='%d' y='%d' width='%d' height='%d' rx='%d' "
             "fill='%s' stroke='%s' stroke-width='1.5'/>"
             % (x, y, w, h, h // 2, fill, stroke)]
    for i, line in enumerate(lines):
        parts.append(_text(x + 14, y + 14 + i * 17 + 5, line, weight="600"))
    return "".join(parts), h


def _address_pill(x, y, label):
    w = max(120, 9 * len(label) + 44)
    h = 34
    return ("<g><rect x='%d' y='%d' width='%d' height='%d' rx='17' "
            "fill='#15202c' stroke='#5f7d9c' stroke-width='1.5'/>"
            "<circle cx='%d' cy='%d' r='5' fill='#5f7d9c'/>%s</g>"
            % (x, y, w, h, x + 16, y + h // 2,
               _text(x + 28, y + h // 2 + 5, "→ " + label)), h)


def _arrow(x1, y1, x2, y2, dashed=False):
    dash = " stroke-dasharray='5 4'" if dashed else ""
    return ("<line x1='%d' y1='%d' x2='%d' y2='%d' stroke='#5f7d9c' "
            "stroke-width='1.4'%s marker-end='url(#a)'/>"
            % (x1, y1, x2, y2, dash))


def svg():
    """Render the silhouette as a standalone SVG document."""
    body, y = [], 70
    connectors = []     # deferred arrows (drawn last, under the boxes)

    for node in TRUNK:
        kind = node[0]
        if kind == "action":
            svg_box, h = _action_box(_TRUNK_X, y, _BOX_W, node[1])
            body.append(svg_box)
            body.append(_arrow(_TRUNK_X + _BOX_W // 2, y + h,
                               _TRUNK_X + _BOX_W // 2, y + h + 26))
            y += h + 26

        elif kind == "terminal":
            svg_box, h = _terminal_box(_TRUNK_X, y, _BOX_W, node[1],
                                       "ok" if node[1] == "ok" else "stop")
            body.append(svg_box)
            y += h + 26

        elif kind == "branch":
            svg_box, h = _branch_box(_TRUNK_X, y, _BOX_W, node[1])
            body.append(svg_box)
            # "yes" leaves to the right, "no" continues down the trunk
            connectors.append(_arrow(_TRUNK_X + _BOX_W, y + h // 2,
                                     _BRANCH_X - 14, y + h // 2))
            body.append(_text(_TRUNK_X + _BOX_W - 40, y + h // 2 - 6, "да",
                              size=12, fill="#8fb3a0"))
            body.append(_text(_TRUNK_X + _BOX_W // 2, y + h + 18, "нет",
                              size=12, anchor="middle", fill="#c08a86"))

            cy = y + h // 2 - 17
            for sub in node[2]:
                if sub[0] == "terminal":
                    b, sh = _terminal_box(_BRANCH_X, cy, _BRANCH_W, sub[1],
                                          "ok" if sub[1] == "ok" else "stop")
                    body.append(b)
                elif sub[0] == "address":
                    b, sh = _address_pill(_BRANCH_X, cy, sub[1])
                    body.append(b)
                elif sub[0] == "action":
                    b, sh = _action_box(_BRANCH_X, cy, _BRANCH_W, sub[1])
                    body.append(b)
                    connectors.append(_arrow(_BRANCH_X + _BRANCH_W // 2,
                                             cy + sh, _BRANCH_X + _BRANCH_W // 2,
                                             cy + sh + 22))
                elif sub[0] == "branch":
                    b, sh = _branch_box(_BRANCH_X, cy, _BRANCH_W, sub[1])
                    body.append(b)
                    body.append(_text(_BRANCH_X + _BRANCH_W - 40,
                                      cy + sh // 2 - 6, "да", size=11,
                                      fill="#8fb3a0"))
                    connectors.append(_arrow(_BRANCH_X + _BRANCH_W // 2,
                                             cy + sh, _BRANCH_X + _BRANCH_W // 2,
                                             cy + sh + 22))
                    cy += sh + 22
                    for sub2 in sub[2]:
                        if sub2[0] == "terminal":
                            b2, sh2 = _terminal_box(_BRANCH_X, cy, _BRANCH_W,
                                                    sub2[1], "stop")
                        else:
                            b2, sh2 = _address_pill(_BRANCH_X, cy, sub2[1])
                        body.append(b2)
                        cy += sh2 + 14
                    cy -= 14
                    sh = 0
                cy += sh + 16

            used = (cy - 16) - (y + h // 2 - 17)
            body.append(_arrow(_TRUNK_X + _BOX_W // 2, y + h,
                               _TRUNK_X + _BOX_W // 2, y + h + 26))
            y += max(h, used) + 34

    height = y + 60
    head = ("<svg xmlns='http://www.w3.org/2000/svg' width='%d' height='%d' "
            "viewBox='0 0 %d %d'>"
            "<defs><marker id='a' viewBox='0 0 10 10' refX='9' refY='5' "
            "markerWidth='6' markerHeight='6' orient='auto-start-reverse'>"
            "<path d='M 0 0 L 10 5 L 0 10 z' fill='#5f7d9c'/></marker></defs>"
            "<rect width='100%%' height='100%%' fill='#0f141b'/>"
            % (_W, height, _W, height))
    title = (_text(_TRUNK_X, 34, "ai_Pobisk — машина состояний управления",
                   size=18, weight="700")
             + _text(_TRUNK_X, 54,
                     "силуэт ДРАКОНа: ствол — шаг задачи, ветви справа — "
                     "пути восстановления", size=12, fill="#8fa3ba"))
    legend = _text(_W - 30, 34, "○ адрес = возврат на ствол", size=12,
                   anchor="end", fill="#8fa3ba")
    return head + title + legend + "".join(connectors) + "".join(body) + "</svg>"


def write_svg(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg())
    return path


def summary():
    """Counts for `self-test` / `drakon`: the machine in one line."""
    return "%d states, %d transitions, %d terminals" % (
        len(STATES), len(TRANSITIONS), len(TERMINAL))
