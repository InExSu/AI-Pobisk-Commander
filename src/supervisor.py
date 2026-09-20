"""supervisor.py — the loop that keeps a task alive.

For each step: pick a worker, run it, read the Outcome, decide what to do.

    ok              -> next step
    retry           -> same worker/model again, capped
    rotate_model    -> another model on the same skill
    rotate_account  -> another account/skill
    question        -> competence check (see competence.py)
    fatal           -> stop the task, report
    loop_guard      -> stop the task, report

The meta model is consulted only when the rule table has no answer, when a
budget is exhausted, or when the outcome is `question`. Every other decision
is deterministic — cheap and predictable.
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, os.path.join(_HERE, "adapters"),
          os.path.join(_ROOT, ".agents", "skills", "_shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

from outcome import (OK, RETRY, ROTATE_MODEL, ROTATE_ACCOUNT,  # noqa: E402
                     FATAL, QUESTION, WAITING, LOOP_GUARD)

# Answer a confirmation prompt on the user's behalf. Phrased to cover the
# shapes agents actually emit (y/N, Enter, "any key") without being so
# verbose that it eats the context budget.
NUDGE = (
    "Automated run — no human is at the keyboard. Do NOT ask for confirmation, "
    "do not wait for input, and do not prompt 'Press Enter to continue' or "
    "'Proceed? [y/N]'. Assume yes: continue the task autonomously, make the "
    "reasonable choice yourself, and run to completion. Report what you did "
    "when finished."
)


def _nudge_prompt(step):
    """Append the autonomy instruction to a step prompt."""
    return "%s\n\n---\n%s" % (step, NUDGE)


def _target_dir_from_task(task):
    """Absolute directory mentioned in a task body, or None.

    Tasks are written as "Прочитать /abs/path/AGENTS.md …" — the first absolute
    path usually names the repo the work belongs to.
    """
    import re
    m = re.search(r"(/Users/[^\s\"'`,)]+|/[a-z][^\s\"'`,)]*/)", task.body or "")
    if not m:
        return None
    p = m.group(1)
    if os.path.isdir(p):
        return p
    parent = os.path.dirname(p)
    return parent if os.path.isdir(parent) else None


class Supervisor:
    def __init__(self, repo_root, cfg, adapters, meta, journal, verbose=True):
        self.repo_root = repo_root
        self.cfg = cfg
        self.adapters = adapters
        self.meta = meta
        self.journal = journal
        self.verbose = verbose
        self.worker_order = [w for w in cfg["workers"]["order"]
                             if w in adapters]
        self.non_workers = set(cfg["workers"]["non_workers"])
        self._tried = {}      # skill -> set of model ids already attempted
        self._graft_ctx = None   # lazy: import graftctx only when enabled
        self._graft_root = None  # resolved once: the repo the task targets

    # ── one task ──────────────────────────────────────────────────────────────
    def run_task(self, task, dry_run=False):
        name = task.name
        st = self.journal.task_state(name)
        self.journal.section("task %s" % name)
        self._say("task %s: %s" % (name, task.title))

        if dry_run:
            for i, step in enumerate(task.steps(), 1):
                self._say("  step %d (dry-run): %s" % (i, step[:100]))
            self.journal.mark(name, "dry-run")
            return "dry-run"

        started = time.time()
        wall = int(self.cfg["budget"]["max_wall_clock_sec"])
        max_steps = int(self.cfg["budget"]["max_steps_per_task"])

        for idx, step in enumerate(task.steps(), 1):
            if idx <= st.get("step", 0):
                continue                      # already done in a previous run
            if time.time() - started > wall:
                self.journal.mark(name, "failed", idx - 1,
                                  "wall clock budget spent (%ds)" % wall)
                self._say("  ABORT: wall clock budget spent")
                return "failed"
            if idx > max_steps:
                self.journal.mark(name, "failed", idx - 1, "too many steps")
                return "failed"

            res = self.run_step(task, idx, step)
            if res == "ok":
                self.journal.mark(name, "running", idx, "step %d ok" % idx)
                continue
            self.journal.mark(name, "failed", idx - 1,
                              "step %d -> %s" % (idx, res))
            return res

        self.journal.mark(name, "done", len(task.steps()), "all steps done")
        self.journal.event("task done", etype="task_end", task=name,
                           result="done",
                           wall_ms=int((time.time() - started) * 1000))
        self._say("  task %s: done" % name)
        return "done"

    # ── one step ──────────────────────────────────────────────────────────────
    def run_step(self, task, idx, step):
        name = task.name
        attempts = self.journal.attempts(name)
        model_rot = 0
        acct_rot = 0
        worker_idx = 0
        model = None

        cap_retry = int(self.cfg["budget"]["max_retries_per_step"])
        cap_model = int(self.cfg["budget"]["max_model_rotations"])
        cap_acct = int(self.cfg["budget"]["max_account_rotations"])

        # Hard iteration cap. The meta model can ask for another attempt, and
        # invariants can rotate; neither may loop without bound. This is the
        # backstop that makes every other rule safe to be wrong about.
        max_iters = int(self.cfg["budget"].get("max_iterations_per_step", 40))
        iters = 0

        while True:
            iters += 1
            if iters > max_iters:
                self._say("  step %d: iteration cap (%d) — stopping" % (idx, max_iters))
                return "loop_guard"
            if worker_idx >= len(self.worker_order):
                self._say("  step %d: no workers left" % idx)
                return "exhausted"
            skill = self.worker_order[worker_idx]
            a = self.adapters[skill]

            self._say("  step %d: %s%s" % (idx, skill,
                                           " (%s)" % model if model else ""))
            prompt = self._with_context(task, step)
            prompt = self._with_cwd(task, prompt)
            o = a.run(prompt, model=model,
                      timeout=int(self.cfg["budget"].get("step_timeout_sec", 600)))
            self._pace(o)
            # Remember which model actually served the call, so a rotation has
            # a real starting point. Skills that pick internally report their
            # choice back in `model`; those that do not leave it empty, and
            # _next_model() then walks the catalogue from the top.
            if not model and o.model:
                model = o.model
            self.journal.event(
                "%s -> %s: %s" % (skill, o.outcome, o.detail[:100]),
                etype="attempt", task=name, step=idx, skill=skill,
                model=model or "", outcome=o.outcome, http=o.http,
                elapsed_ms=o.elapsed_ms,
                attempt_no=attempts.get("attempt", 0))

            if o.outcome == OK:
                return "ok"

            # ── INVARIANT FIRST ───────────────────────────────────────────────
            # Everything below is a hard rule the model must NOT get a vote on.
            # The meta model decides the *interesting* cases; the code decides
            # the ones where being wrong is unrecoverable or absurd.
            inv = self._invariant(o, attempts, cap_retry)
            if inv:
                self.journal.event("invariant: %s" % inv, etype="invariant",
                                   task=name, step=idx, skill=skill,
                                   directive=inv, outcome=o.outcome)
                self._say("    invariant: %s" % inv)
                if inv == "stop_budget":
                    return "exhausted"
                if inv == "stop_loop":
                    return "loop_guard"
                if inv == "rotate_model":
                    model_rot += 1
                    if model_rot > cap_model:
                        worker_idx += 1
                        model = None
                        model_rot = 0
                        continue
                    model = self._next_model(skill, model)
                    self.journal.bump(name, "model")
                    continue
                if inv == "rotate_account":
                    acct_rot += 1
                    if acct_rot > cap_acct:
                        return "exhausted"
                    worker_idx += 1
                    model = None
                    self.journal.bump(name, "account")
                    continue

            # ── confirmations: never a meta question ──
            if o.outcome == WAITING:
                # The model stopped on a confirmation prompt. Nobody is at the
                # keyboard, so ai_Pobisk answers for the user and tells it to
                # carry on. Capped: a model stuck in a confirmation loop would
                # otherwise be nudged forever.
                n = self.journal.bump(name, "nudge")
                if n > int(self.cfg["budget"].get("max_nudges_per_step", 5)):
                    self._say("    stuck on prompts (%d nudges) — stopping" % n)
                    return "loop_guard"
                self._say("    model waits for input — nudging (attempt %d)" % n)
                self.journal.event("nudge: %s" % o.detail[:80], etype="nudge",
                                   task=name, step=idx, skill=skill,
                                   attempt_no=n)
                step = _nudge_prompt(step)
                continue

            if o.outcome == QUESTION:
                answer = self._handle_question(task, o)
                if answer is None:
                    return "needs_owner"
                step = "%s\n\nAdditional context from ai_Pobisk: %s" % (step, answer)
                continue

            if o.outcome == LOOP_GUARD:
                self._say("    loop guard — stopping step")
                return "loop_guard"

            # ── the model decides ─────────────────────────────────────────────
            # Every case the invariants did not settle goes to the meta model:
            # unrecognised errors, weird half-answers, new provider messages,
            # anything the rule table never heard of. It must answer with one
            # of the agreed verdicts; the code enforces that and the budgets.
            verdict, conf, reason = self.meta.decide(
                o, {"task": name, "step": idx, "skill": skill,
                    "tried": sorted(self._tried.get(skill, set()))}, attempts)
            self.journal.event("meta: %s conf=%.2f (%s)" % (verdict, conf, reason),
                               etype="meta", task=name, step=idx, skill=skill,
                               verdict=verdict, confidence=conf,
                               outcome=o.outcome)
            self._say("    meta: %s (%.2f) %s" % (verdict, conf, reason))

            floor = float(self.cfg["fallback"]["fatal_confidence_floor"])
            if verdict == FATAL and conf >= floor:
                return "fatal"
            if verdict == FATAL and conf < floor:
                verdict = ROTATE_ACCOUNT      # unsure: try elsewhere

            if verdict == RETRY:
                n = self.journal.bump(name, "retry")
                if n <= cap_retry:
                    continue
                model_rot += 1
                model = self._next_model(skill, model)
                continue
            if verdict == ROTATE_MODEL:
                model_rot += 1
                model = self._next_model(skill, model)
                continue
            if verdict == ROTATE_ACCOUNT:
                acct_rot += 1
                worker_idx += 1
                model = None
                continue
            return "fatal"

    # ── helpers ───────────────────────────────────────────────────────────────
    def _invariant(self, o, attempts, cap_retry):
        """Hard rules the meta model may NOT override. Returns a directive.

        The meta model handles the long tail — the infinite variety of ways a
        model can stall, refuse, or half-answer, which no rule table can
        enumerate. But some things must never be negotiable:

        * budgets — a model that says "retry" forever would run forever
        * credential/wallet failures — retrying cannot fix them
        * "no filesystem" — the worker lacks the capability, not the luck
        * confirmations — nobody is at the keyboard, so the answer is always
          "continue"; asking a model whether to ask the owner is absurd
        * loop_guard — already spinning, stop

        Returns one of: rotate_model, rotate_account, stop_budget, stop_loop,
        or None ("not my call — let the meta model decide").
        """
        # Already spinning: stop, no discussion.
        if o.outcome == LOOP_GUARD:
            return "stop_loop"

        # Nobody at the keyboard: answer "continue" and carry on. Asking the
        # meta model whether to involve the owner here is nonsense.
        if o.outcome == WAITING:
            return None      # handled by the nudge branch below

        # Budget spent: retrying further is an infinite loop.
        if o.outcome == RETRY and attempts.get("retry", 0) >= cap_retry:
            return "rotate_model"

        # Credential / wallet / capability failures are never retryable.
        if o.outcome == ROTATE_ACCOUNT:
            return "rotate_account"

        # A bare completion saying "I cannot access your files" is a
        # capability gap, not bad luck — no amount of asking changes it.
        if "no file access" in (o.detail or "").lower():
            return "rotate_account"

        return None

    def _pace(self, outcome):
        """Sleep when attempts come back implausibly fast.

        A broken skill (missing binary, bad path) fails in milliseconds. The
        supervisor would then burn its whole retry budget in a second and
        hammer the same dead end. Real work takes seconds at least; anything
        faster is a configuration failure, so slow it down.
        """
        floor_ms = int(self.cfg["budget"].get("min_step_ms", 2000))
        if outcome.elapsed_ms < floor_ms and outcome.outcome != "ok":
            wait = (floor_ms - outcome.elapsed_ms) / 1000.0
            time.sleep(min(wait, 5.0))

    def _with_cwd(self, task, prompt):
        """Tell the worker which directory the task is about.

        Task files often start with "Прочитать /abs/path/AGENTS.md" but never
        say where the work happens. An agent that runs in the repo root of
        ai_Pobisk instead of the target repo reads the wrong files — or
        claims it cannot find them.
        """
        if self.cfg.get("graft", {}).get("repo"):
            target = self.cfg["graft"]["repo"]
        else:
            target = _target_dir_from_task(task) or self.repo_root
        return ("Work in this directory: %s\n\n%s" % (target, prompt))

    def _with_context(self, task, step):
        """Prepend a code-graph context block to the worker prompt.

        The worker starts with a map instead of re-exploring the repo:
        graft measures ~42% fewer tokens and ~46% fewer tool calls for the
        same correctness. Opt-in via config; a missing graft or an unbuilt
        graph returns the prompt untouched, so this can never break a run.
        """
        g = self.cfg.get("graft") or {}
        if not g.get("enabled"):
            return step
        # Map the repo the TASK is about, not the repo ai_Pobisk lives in.
        # With repo = "" the worker got a map of ai_Pobisk's own src/ while
        # being asked to add games to igrasu.ru — context that actively
        # misleads. The task's target directory decides the graph.
        if self._graft_root is None:
            explicit = (g.get("repo") or "").strip()
            self._graft_root = explicit or _target_dir_from_task(task) or self.repo_root
        root = self._graft_root
        if self._graft_ctx is None:
            try:
                import graftctx
                self._graft_ctx = graftctx
            except Exception:
                self._graft_ctx = False
        if not self._graft_ctx:
            return step
        block = self._graft_ctx.context_for(root, step)
        if not block:
            return step
        self.journal.event("graft context: %d chars" % len(block))
        return "%s\n\n---\n%s" % (block, step)

    def _next_model(self, skill, current):
        """Another model on the same skill, or None to let the skill choose.

        `current` may be None when the skill picked internally and did not say
        which model it used. Falling back to models[0] would re-run the very
        model that just failed, so track the tried ones and advance past them.
        """
        a = self.adapters.get(skill)
        if not a or not hasattr(a, "models"):
            return None
        models = a.models()
        if not models:
            return None
        if current and current in models:
            i = models.index(current)
            nxt = models[(i + 1) % len(models)]
        else:
            tried = self._tried.setdefault(skill, set())
            if current:
                tried.add(current)
            nxt = None
            for m in models:
                if m not in tried:
                    nxt = m
                    break
            if nxt is None:
                tried.clear()          # every model tried: start over once
                nxt = models[0]
        if nxt:
            self._tried.setdefault(skill, set()).add(nxt)
        return nxt

    def _handle_question(self, task, outcome):
        """Ask competence.py whether ai_Pobisk can answer this itself."""
        try:
            from competence import answer_question
            return answer_question(self.repo_root, self.cfg, self.meta,
                                   outcome.detail, self.journal, task.name)
        except Exception as e:
            self.journal.event("question handling failed: %s" % type(e).__name__)
            return None

    def _say(self, text):
        if self.verbose:
            print(text)
