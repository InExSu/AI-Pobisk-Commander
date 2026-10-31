"""supervisor.py — the loop that keeps a task alive.

The decisions are not made here. They live in `statemachine.py` as one table
of states and transitions, and this module only performs the side effects each
transition implies: call an adapter, bump a counter, rotate a model, ask the
meta model. That split is deliberate — the DRAKON silhouette in
`./ai_Pobisk.sh drakon` is generated from the same table, so the picture and
the code cannot drift apart.

    dispatch   -> call the adapter
    ok         -> next step
    retry      -> same worker/model again, capped
    rotate_model    -> another model on the same skill
    rotate_account  -> another account/skill
    question   -> competence check (see competence.py)
    fatal      -> stop the task, report
    loop_guard -> stop the task, report

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

import statemachine as sm       # noqa: E402

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

# Words that mean "a file or a repo is involved". Deliberately narrow: a false
# positive only costs a cheap worker, while a false negative is caught later
# by classify(), when the worker admits it cannot see the files. So this list
# names files and version control — not "code" or "translate", which appear in
# plenty of pure text tasks that a completion API handles perfectly well.
FILE_TASK_KEYWORDS = (
    "файл", "репозитор", "репозиторий", "коммит", "пуш", "ветк", "папк",
    "директор", "git",
    "file", "repo", "commit", "push", "branch", "folder", "directory",
    "filename",
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
        self._ctx_cache = {}     # task name -> context block ("" = none)
        self._sync_no_filesystem()

    def _sync_no_filesystem(self):
        """Hand the configured no-filesystem list to classify.

        The supervisor is the only holder of the config, and classify() is the
        only place that can recognise "I cannot access your files". This is
        where the two meet, so the TOML has exactly one list of skills and
        patterns and classify has exactly one copy of the rule.
        """
        try:
            import classify as _classify
            nfs = (self.cfg.get("workers", {}) or {}).get("no_filesystem") or {}
            _classify.set_no_filesystem(nfs.get("skills"), nfs.get("patterns"))
        except Exception:
            pass          # defaults in classify.py stand in for the config

    # ── one task ──────────────────────────────────────────────────────────────
    def run_task(self, task, dry_run=False):
        name = task.name
        st = self.journal.task_state(name)
        self.journal.section("task %s" % name)
        self._say("task %s: %s" % (name, task.subtitle[:90]))

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
        """Drive one step through the control machine.

        The machine says *what* to do; this method does it. Every branch below
        is the side effect of a state the machine chose — there are no
        decisions left in this function.
        """
        name = task.name
        attempts = self.journal.attempts(name)
        model_rot = 0
        acct_rot = 0
        worker_idx = 0
        model = None

        # Capability routing: a bare completion API has no filesystem, so a
        # step that edits files must never be handed to one. The filtered list
        # is what this step walks.
        order = self._eligible_workers(task, step, idx)

        budgets = {
            "max_retries": int(self.cfg["budget"]["max_retries_per_step"]),
            "max_model_rot": int(self.cfg["budget"]["max_model_rotations"]),
            "max_acct_rot": int(self.cfg["budget"]["max_account_rotations"]),
            # Hard iteration cap. The meta model can ask for another attempt,
            # and invariants can rotate; neither may loop without bound. This
            # is the backstop that makes every other rule safe to be wrong.
            "max_iters": int(self.cfg["budget"].get("max_iterations_per_step", 40)),
            "max_nudges": int(self.cfg["budget"].get("max_nudges_per_step", 5)),
            "fatal_floor": float(self.cfg["fallback"]["fatal_confidence_floor"]),
            # Counts the workers that could actually do THIS step, not every
            # worker in the list: a file step never reaches the API workers,
            # so "no workers left" must not wait for them either.
            "n_workers": len(order),
            # How many times the supervisor may re-open the fleet before it
            # accepts that there are no hands left.
            "max_rediscoveries": int(
                self.cfg["budget"].get("max_rediscoveries", 2)),
        }

        iters = 0
        rediscoveries = 0
        while True:
            iters += 1
            counters = {
                "iters": iters,
                "worker_idx": worker_idx,
                "retry": attempts.get("retry", 0),
                "nudges": attempts.get("nudge", 0),
                "model_rot": model_rot,
                "acct_rot": acct_rot,
            }

            # ── pre-call guards: no worker is hired when the run is out ──────
            d = sm.guard(counters, budgets)
            if (d and d.state == sm.EXHAUSTED_ST
                    and rediscoveries < budgets["max_rediscoveries"]):
                # "No workers left" is a claim about the list we just walked,
                # not about the fleet: most of it is usually sitting in a
                # 15-minute cooldown. Lift the pauses, then walk it again.
                rediscoveries += 1
                if self._find_hands(name, idx):
                    worker_idx = 0
                    model = None
                    continue
            if d:
                self._say("  step %d: %s" % (idx, d.reason))
                return d.result

            skill = order[worker_idx]
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

            # ── the machine decides ──────────────────────────────────────────
            d = sm.decide(o, counters, budgets)
            if d.state == sm.META:
                # Only the long tail gets here: errors the rule table never
                # heard of. The meta model must answer with one of the agreed
                # verdicts; the code still owns the budgets.
                verdict, conf, reason = self.meta.decide(
                    o, {"task": name, "step": idx, "skill": skill,
                        "tried": sorted(self._tried.get(skill, set()))}, attempts)
                self.journal.event(
                    "meta: %s conf=%.2f (%s)" % (verdict, conf, reason),
                    etype="meta", task=name, step=idx, skill=skill,
                    verdict=verdict, confidence=conf, outcome=o.outcome)
                self._say("    meta: %s (%.2f) %s" % (verdict, conf, reason))
                d = sm.resolve_meta(verdict, conf, counters, budgets)
            elif d.state in (sm.LOOP_ST, sm.EXHAUSTED_ST):
                self.journal.event("invariant: %s" % d.reason,
                                   etype="invariant", task=name, step=idx,
                                   skill=skill, directive=d.event,
                                   outcome=o.outcome)
                self._say("    invariant: %s" % d.reason)

            if d.terminal:
                self._say("    -> %s (%s)" % (d.state, d.reason))
                return d.result

            if d.state == sm.NUDGE:
                # The model stopped on a confirmation prompt. Nobody is at the
                # keyboard, so ai_Pobisk answers for the user and tells it to
                # carry on. Capped: a model stuck in a confirmation loop would
                # otherwise be nudged forever.
                n = self.journal.bump(name, "nudge")
                self._say("    model waits for input — nudging (attempt %d)" % n)
                self.journal.event("nudge: %s" % o.detail[:80], etype="nudge",
                                   task=name, step=idx, skill=skill,
                                   attempt_no=n)
                step = _nudge_prompt(step)
                continue

            if d.state == sm.QUESTION_ST:
                answer = self._handle_question(task, o)
                if answer is None:
                    return "needs_owner"
                step = "%s\n\nAdditional context from ai_Pobisk: %s" % (step, answer)
                continue

            if d.state == sm.RETRY_ST:
                self.journal.bump(name, "retry")
                continue

            if d.state == sm.ROTATE_MODEL_ST:
                model_rot += 1
                if model_rot > budgets["max_model_rot"]:
                    worker_idx += 1
                    model = None
                    model_rot = 0
                    continue
                model = self._next_model(skill, model)
                self.journal.bump(name, "model")
                continue

            if d.state == sm.ROTATE_ACCOUNT_ST:
                acct_rot += 1
                if acct_rot > budgets["max_acct_rot"]:
                    return "exhausted"
                worker_idx += 1
                model = None
                self.journal.bump(name, "account")
                continue

            # Unknown state: a machine bug, not a model bug. Stop rather than
            # spin on a transition nobody defined.
            self._say("    unknown state %r — stopping" % d.state)
            return "loop_guard"

    # ── helpers ───────────────────────────────────────────────────────────────
    def _no_filesystem_skills(self):
        """Workers that cannot touch files on disk.

        Two sources, unioned. The adapter knows the truth (nvidia-rotate.sh
        posts to an HTTP endpoint — nothing about that can change), and the
        config lets the owner exclude a worker without editing Python. A
        worker is excluded when EITHER source says so, so the only direction
        this can move is "away from file work"; it can never accidentally
        hand file work to a worker that cannot do it.
        """
        out = set(self.cfg.get("workers", {})
                  .get("no_filesystem", {})
                  .get("skills", ()) or ())
        for name, a in self.adapters.items():
            if not getattr(a, "has_filesystem", True):
                out.add(name)
        return out

    def _requires_filesystem(self, task, step):
        """Does this step need to change files on disk?

        Three signals, most precise first:

        1. An explicit directive in the task body — ``# requires: filesystem``
           or ``# requires: text``. The author of the task always wins.
        2. The task names a directory that exists: that is the repo the work
           belongs to, so the work happens in files.
        3. File or version-control vocabulary in the text.

        Signals 2 and 3 are heuristics, but both ways of being wrong are
        harmless: a text task misread as a file task merely skips the API
        workers, and a file task misread as a text task is caught later by
        classify(), when the worker answers "I cannot access your files".
        """
        body = (getattr(task, "body", "") or "")
        low = ("%s\n%s" % (body, step or "")).lower()

        if "requires: filesystem" in low or "requires: fs" in low:
            return True
        if "requires: text" in low or "requires: no-filesystem" in low:
            return False

        if task is not None and _target_dir_from_task(task):
            return True

        keywords = self.cfg.get("capabilities", {}).get("file_task_keywords")
        if keywords is None:
            keywords = FILE_TASK_KEYWORDS
        return any(k in low for k in keywords)

    def _eligible_workers(self, task, step, idx=0):
        """The workers this step may be handed to, in rotation order.

        For a file step, workers without a filesystem are dropped: a bare
        completion API cannot edit anything, it can only describe. For a text
        step every worker stays eligible — that is what those APIs are for.
        """
        if not self._requires_filesystem(task, step):
            return list(self.worker_order)

        no_fs = self._no_filesystem_skills()
        order = [w for w in self.worker_order if w not in no_fs]
        if not order:
            # Never strand a task: if every worker claims to be
            # filesystem-less, walk the full list and let the rules decide.
            return list(self.worker_order)
        skipped = [w for w in self.worker_order if w in no_fs]
        if skipped:
            self._say("    файловый шаг — пропускаю без ФС: %s"
                      % ", ".join(skipped))
            try:
                self.journal.event(
                    "file step: skipped %s (no filesystem)"
                    % ", ".join(skipped),
                    etype="capability", task=getattr(task, "name", ""),
                    step=idx, skipped=",".join(skipped))
            except Exception:
                pass
        return order

    def _find_hands(self, task_name, idx):
        """Lift transient cooldowns and look at the fleet again.

        Returns the number of models made reachable. `auth_error` entries are
        left parked on purpose — a bad key does not heal by waiting — so a
        fleet that is genuinely broken still ends in `exhausted`.
        """
        try:
            import hands
            revived = hands.revive(hands.HEALTH_STORE, self.worker_order)
        except Exception as e:
            self._say("    hands: lookup failed (%s)" % type(e).__name__)
            return 0
        n = sum(revived.values())
        if n:
            # Start model rotation from the top of the catalogue again; the
            # old tried-set only remembers models this step already burned.
            self._tried.clear()
            self.journal.event(
                "hands: revived %d model(s) in %s"
                % (n, ", ".join(sorted(revived))),
                etype="hands", task=task_name, step=idx, revived=n)
            self._say("    все воркеры вышли — ищу руки заново: %d моделей" % n)
        return n

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

        The worker starts with a map instead of re-exploring the repo.
        Opt-in via config; a missing graft or an unbuilt graph returns the
        prompt untouched, so this can never break a run.

        Computed once per task and cached: `_with_context` runs on every loop
        iteration, and a step can be retried or nudged dozens of times. Two
        subprocess calls per iteration would dominate the run — and for a repo
        with no graph they would all return nothing anyway.
        """
        g = self.cfg.get("graft") or {}
        if not g.get("enabled"):
            return step

        block = self._ctx_cache.get(task.name)
        if block is None:
            # Map the repo the TASK is about, not the repo ai_Pobisk lives in.
            # With repo = "" the worker got a map of ai_Pobisk's own src/ while
            # being asked to add games to igrasu.ru — context that actively
            # misleads. Resolved per task, not once per run.
            explicit = (g.get("repo") or "").strip()
            root = explicit or _target_dir_from_task(task) or self.repo_root
            if self._graft_ctx is None:
                try:
                    import graftctx
                    self._graft_ctx = graftctx
                except Exception:
                    self._graft_ctx = False
            block = ""
            if self._graft_ctx:
                block = self._graft_ctx.context_for(root, task.body or step)
            self._ctx_cache[task.name] = block
            if block:
                self.journal.event("graft context: %d chars" % len(block),
                                   etype="graft", task=task.name)

        if not block:
            return step
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
