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
import json
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, os.path.join(_HERE, "adapters"),
          os.path.join(_ROOT, ".agents", "skills", "_shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import statemachine as sm       # noqa: E402
from config import load_model_ranking  # noqa: E402

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
    # realpath/abspath so "repo/" and "repo" and "repo/./" are ONE directory:
    # same_repo_guard compares these strings to decide serial-vs-parallel, and
    # a trailing slash once defeated it — three tasks landed on one repo at
    # the same time.
    p = os.path.realpath(os.path.abspath(os.path.expanduser(p)))
    if os.path.isdir(p):
        return p
    parent = os.path.dirname(p)
    return parent if os.path.isdir(parent) else None


def _reconcile_task_state(journal, task):
    """A task file that changed is a new task, even if its name is the same.

    state.json is keyed by file name, so editing an instruction used to be
    invisible: `run_task` resumed at the recorded step and printed "done" in
    one second. That is how a rewritten task got skipped three times in a row.
    """
    st = journal.task_state(task.name)
    h = task.body_hash
    old = st.get("body_hash")
    if old and old != h:
        journal.note(task.name,
                     "task body changed (%s -> %s) — restarting from step 0"
                     % (old, h))
        journal.mark(task.name, "pending", 0)
        st = journal.task_state(task.name)
    st["body_hash"] = h
    journal.save()
    return st


def _target_repo_dir(task, cfg):
    """Directory the worker will actually edit — where evidence lives.

    Mirrors `_with_cwd`: the graft repo wins, else the absolute path named in
    the task body, else None. A None result means "no git repo to watch", and a
    file step there is never falsely flagged for lacking evidence.
    """
    g = (cfg.get("graft") or {}) if cfg else {}
    if g.get("repo"):
        return g["repo"]
    return _target_dir_from_task(task) or None


def _repo_snapshot(repo_dir):
    """Fingerprint of a git repo: HEAD plus full working-tree status.

    Returns None when the path is not a git repo, so a non-git target is never
    falsely flagged. Any real work — a commit, an edit, or a new untracked file
    — changes this string.
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        return None
    import subprocess as _sp
    try:
        head = _sp.run(["git", "-C", repo_dir, "rev-parse", "HEAD"],
                      capture_output=True, text=True, timeout=20)
        if head.returncode != 0:
            return None
        st = _sp.run(["git", "-C", repo_dir, "status", "--porcelain", "-uall"],
                    capture_output=True, text=True, timeout=20)
        return "%s|%s" % (head.stdout.strip(), st.stdout)
    except Exception:
        return None


class _Status:
    """Best-effort single-file run status for an outside observer.

    One read of .ai_pobisk/status.json answers "is it alive / what is it
    doing?" — no process archaeology. Callers pass only what changed; the rest
    is merged over the previous content. Never raises: observability must not
    be able to break a run.
    """

    # One status file, many task threads (parallel run): serialize the
    # read-merge-write cycle, or two threads interleave and the last writer
    # silently drops the other's update.
    _lock = threading.Lock()

    def __init__(self, status_dir):
        self.dir = status_dir
        self.path = os.path.join(status_dir, "status.json")

    def write(self, last_activity, **kw):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with self._lock:
                cur = {}
                if os.path.exists(self.path):
                    try:
                        with open(self.path) as f:
                            cur = json.load(f)
                    except Exception:
                        cur = {}
                cur.update(kw)
                cur["last_activity"] = last_activity
                cur["updated_at"] = time.time()
                # Unique tmp per writer: same reasoning as Journal.save — a
                # fixed name turns concurrent writes into FileNotFoundError
                # or a torn read on the observer's side.
                tmp = "%s.%d.%d.tmp" % (self.path, os.getpid(),
                                        threading.get_ident())
                with open(tmp, "w") as f:
                    json.dump(cur, f, indent=2)
                os.replace(tmp, self.path)
        except Exception:
            pass

    def fold_and_clear_heartbeat(self, last_activity):
        """Pull the worker's liveness beacon into last_activity, then remove it.

        The rotate script writes PB_STATUS_DIR/worker_heartbeat.json on every
        sign of life; folding its last_active gives the most precise "when did
        anything happen" timestamp, and removing the file stops a stale beacon
        from masking the next step's silence.
        """
        try:
            hb = os.path.join(self.dir, "worker_heartbeat.json")
            if os.path.exists(hb):
                try:
                    with open(hb) as f:
                        d = json.load(f)
                    la = d.get("last_active")
                    if isinstance(la, (int, float)) and la > last_activity:
                        last_activity = la
                except Exception:
                    pass
                try:
                    os.remove(hb)
                except Exception:
                    pass
        except Exception:
            pass
        self.write(last_activity)
        return last_activity


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
        self._no_evidence = False  # a file step reported ok but changed nothing
        self._status_last_activity = time.time()
        # The status file lives in the configured state dir, next to the
        # journal. It used to be hardcoded to ".ai_pobisk", which meant a test
        # run overwrote the live status.json an outside observer was reading
        # — and a stale "model" from a fake adapter showed up as if it were
        # the model serving the real task.
        self.status_dir = os.path.join(
            self.repo_root,
            (cfg.get("state") or {}).get("dir") or ".ai_pobisk")
        self.status = _Status(self.status_dir)
        # Workers inherit this so they can write a liveness beacon the
        # supervisor — and a human — can read without process archaeology.
        os.environ["PB_STATUS_DIR"] = self.status_dir
        self._sync_no_filesystem()
        # ── Model quality rating for coding ──────────────────────────────
        # models_for_coding.toml maps model_id -> score. Higher = better.
        # Unknown models fall back to default.score (50). Used by
        # _ranked_models() to try better models first on every step.
        self._model_ranking, self._model_default = load_model_ranking(repo_root)
        if self._model_ranking:
            self._say("  model rating: %d models loaded" % len(self._model_ranking))

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
        st = _reconcile_task_state(self.journal, task)
        self.journal.section("task %s" % name)
        self._say("task %s: %s" % (name, task.subtitle[:90]))

        if dry_run:
            for i, step in enumerate(task.steps(), 1):
                self._say("  step %d (dry-run): %s" % (i, step[:100]))
            self.journal.mark(name, "dry-run")
            return "dry-run"

        # Observability + durability: publish a status file an outside observer
        # can read in one go, and warn if a previous run was left mid-step.
        self._status_last_activity = time.time()
        self._recover_if_crashed(name)
        self.status.write(self._status_last_activity, state="running", task=name,
                          run_pid=os.getpid(), started_at=time.time(),
                          current_step=0, step_state="idle",
                          # A previous run's finish time and verdict describe
                          # a run that is over. Carrying them into a live run
                          # makes status.json claim the task finished before
                          # it started.
                          finished_at=None, last_result=None)

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
                self.status.write(self._status_last_activity, state="failed",
                                 finished_at=time.time(),
                                 step_state="failed", reason="wall clock")
                return "failed"
            if idx > max_steps:
                self.journal.mark(name, "failed", idx - 1, "too many steps")
                self.status.write(self._status_last_activity, state="failed",
                                 finished_at=time.time(),
                                 step_state="failed", reason="too many steps")
                return "failed"

            res = self.run_step(task, idx, step)
            if res == "ok":
                self.journal.mark(name, "running", idx, "step %d ok" % idx)
                continue
            self.journal.mark(name, "failed", idx - 1,
                              "step %d -> %s" % (idx, res))
            self.status.write(self._status_last_activity, state="failed",
                             finished_at=time.time(), step_state="failed",
                             last_result=res,
                             no_evidence=self._no_evidence)
            return res

        self.journal.mark(name, "done", len(task.steps()), "all steps done")
        self.journal.event("task done", etype="task_end", task=name,
                           result="done",
                           wall_ms=int((time.time() - started) * 1000))
        self._say("  task %s: done" % name)
        self.status.write(self._status_last_activity, state="done",
                         finished_at=time.time(), step_state="done",
                         no_evidence=self._no_evidence)
        return "done"

    # ── durability: pick up after a run that died mid-step ─────────────────────
    def _recover_if_crashed(self, name):
        """Clean up the wreckage of a previous run that did not exit cleanly.

        The journal is the source of truth for *which* step to resume from —
        it records every completed step, so `run_task` simply skips them. But
        a run that died mid-step also leaves a frozen `status.json`
        (state=="running") and possibly a `worker_heartbeat.json` beacon. A
        fresh supervisor must not read a heartbeat written by a corpse, and a
        human reading status.json must not be told a dead run is alive.
        """
        try:
            hb = os.path.join(self.status_dir, "worker_heartbeat.json")
            if os.path.exists(hb):
                try:
                    os.remove(hb)
                except Exception:
                    pass

            sp = os.path.join(self.status_dir, "status.json")
            if not os.path.exists(sp):
                return
            try:
                with open(sp) as f:
                    cur = json.load(f)
            except Exception:
                return

            if cur.get("state") == "running":
                pid = cur.get("run_pid")
                if isinstance(pid, int) and os.path.exists("/proc/%d" % pid):
                    self._say("    WARN: status.json frozen on 'running' but "
                              "pid %d is still alive — another supervisor?"
                              % pid)
                else:
                    self.journal.event(
                        "recover: previous run of %s left mid-step "
                        "(status.json frozen on 'running')" % name,
                        etype="recover", task=name,
                        last_step=cur.get("current_step"))
                    self._say("    recover: предыдущий запуск %s упал "
                              "посреди шага %s" % (name, cur.get("current_step")))
        except Exception:
            pass

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

        # Evidence + observability setup for this step. A file step that
        # reports ok but leaves the target repo unchanged is a false positive
        # — a weak model that returned rc=0 having done nothing. Capture the
        # repo fingerprint now so run_step can compare it after the step ends.
        target_dir = _target_repo_dir(task, self.cfg)
        is_file_step = self._requires_filesystem(task, step)
        baseline = _repo_snapshot(target_dir) if is_file_step else None
        self._status_last_activity = time.time()
        self.status.write(self._status_last_activity, state="running",
                         task=name, current_step=idx, step_state="running",
                         worker="", step_started_at=time.time())

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
            # Publish which worker owns this attempt so an outside observer
            # reading status.json sees the active hand, not just "running".
            self.status.write(self._status_last_activity, worker=skill)

            # Pick the best model by coding rating for the first attempt on
            # this worker; later rotations walk the same sorted list.
            if model is None:
                ranked = self._ranked_models(skill, a.models() if hasattr(a, "models") else [])
                model = ranked[0] if ranked else None

            self._say("  step %d: %s%s" % (idx, skill,
                                           " (%s)" % model if model else ""))
            prompt = self._with_context(task, step)
            prompt = self._with_cwd(task, prompt)
            # Run the worker INSIDE the target project (if this task names one),
            # so the agent edits the project and not ai_Pobisk's own tree. The
            # target is per-THREAD, not per-process: two tasks in parallel set
            # different targets, and a shared os.environ export let task A's
            # export point task B's worker at A's repository.
            target = (self.cfg.get("graft") or {}).get("repo") \
                or _target_dir_from_task(task)
            import base as _adapter_base
            _adapter_base.set_target_dir(target or None)
            # Count the attempt before it starts: if the worker dies mid-call
            # the counter must already know an attempt happened. This key was
            # never bumped before, so every attempt event in the journal
            # carried a permanent attempt_no of 0 and "which try was this?"
            # had no answer.
            attempt_no = self.journal.bump(name, "attempt")
            o = a.run(prompt, model=model,
                      timeout=int(self.cfg["budget"].get("step_timeout_sec", 600)))
            self._pace(o)
            # Fold the worker's liveness beacon (written by the rotate script
            # on every sign of life) into last_activity, then drop the file so
            # a stale beacon cannot mask the next attempt's silence.
            self._status_last_activity = self.status.fold_and_clear_heartbeat(
                self._status_last_activity)
            # Remember which model actually served the call, so a rotation has
            # a real starting point. Skills that pick internally report their
            # choice back in `model`; those that do not leave it empty, and
            # _next_model() then walks the catalogue from the top.
            if not model and o.model:
                model = o.model
            # Publish the model that served. status.json is the only window an
            # outside observer has into a run that lasts minutes per step.
            self.status.write(self._status_last_activity,
                              model=o.model or model or "")
            self.journal.event(
                "%s -> %s: %s" % (skill, o.outcome, o.detail[:100]),
                etype="attempt", task=name, step=idx, skill=skill,
                model=o.model or model or "", outcome=o.outcome, http=o.http,
                elapsed_ms=o.elapsed_ms, attempt_no=attempt_no)

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
                # Evidence check — a false positive the rules cannot see.
                # A file step that reports ok but leaves the target repo
                # byte-for-byte as it found it (HEAD and working tree
                # unchanged) is a weak model that returned rc=0 having done
                # no work. Not fatal, and not final either: one live run
                # stalled in needs_review while every other model in the
                # fleet was healthy. The step goes to the NEXT model/worker
                # (bounded by max_no_evidence_per_step); only when the budget
                # runs out does it escalate to a human. The check is skipped
                # when baseline is None — a non-git target we cannot
                # fingerprint is never falsely accused.
                if (d.result == "ok" and is_file_step
                        and baseline is not None):
                    after = _repo_snapshot(target_dir)
                    if after == baseline:
                        no_ev = self.journal.bump(name, "no_evidence")
                        max_no_ev = int(
                            self.cfg["budget"].get(
                                "max_no_evidence_per_step", 3))
                        if no_ev <= max_no_ev:
                            self.journal.event(
                                "evidence: ok без работы (%s) — ротация, "
                                "попытка %d/%d" % (target_dir, no_ev,
                                                   max_no_ev),
                                etype="evidence", task=name, step=idx,
                                skill=skill, attempt_no=no_ev)
                            self._say("    evidence: шаг %d вернул ok, но "
                                      "репозиторий %s не изменился — "
                                      "другая модель (%d/%d)"
                                      % (idx, target_dir, no_ev, max_no_ev))
                            nxt = self._next_model(skill, o.model or model)
                            model_rot += 1
                            if model_rot > budgets["max_model_rot"] or not nxt:
                                worker_idx += 1
                                model = None
                                model_rot = 0
                            else:
                                model = nxt
                            self.journal.bump(name, "model")
                            continue
                        self._no_evidence = True
                        self.journal.event(
                            "evidence: step %d reported ok but %s unchanged"
                            % (idx, target_dir),
                            etype="evidence", task=name, step=idx,
                            skill=skill)
                        self._say("    evidence: шаг %d вернул ok, но "
                                  "репозиторий %s не изменился — нужна "
                                  "проверка" % (idx, target_dir))
                        return "needs_review"
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

    def _ranked_models(self, skill, models):
        """Return `models` sorted by coding rating (highest first).

        Unknown models fall back to default.score. Models already tried on
        this step are kept but pushed to the end — the supervisor has already
        rejected them once, so they are a last resort.
        """
        if not models:
            return []
        ranking = self._model_ranking
        default_score = self._model_default.get("score", 50)
        tried = self._tried.get(skill, set())

        def sort_key(m):
            score = ranking.get(m, default_score)
            # Tried models are deprioritised: they already failed this step.
            penalty = 1000 if m in tried else 0
            return (-score, penalty)

        return sorted(models, key=sort_key)

    def _next_model(self, skill, current):
        """Another model on the same skill, or None to let the skill choose.

        `current` may be None when the skill picked internally and did not say
        which model it used. Models are returned in coding-rating order
        (highest first), so the best available model is tried next.
        """
        a = self.adapters.get(skill)
        if not a or not hasattr(a, "models"):
            return None
        models = a.models()
        if not models:
            return None
        ranked = self._ranked_models(skill, models)
        if current and current in ranked:
            i = ranked.index(current)
            nxt = ranked[(i + 1) % len(ranked)]
        else:
            tried = self._tried.setdefault(skill, set())
            if current:
                tried.add(current)
            nxt = None
            for m in ranked:
                if m not in tried:
                    nxt = m
                    break
            if nxt is None:
                tried.clear()          # every model tried: start over once
                nxt = ranked[0]
        if nxt:
            self._tried.setdefault(skill, set()).add(nxt)
        return nxt

    def _handle_question(self, task, outcome):
        """Ask competence.py whether ai_Pobisk can answer this itself."""
        try:
            from competence import answer_question
            # The QUESTION outcome's detail is only the FIRST line of the
            # transcript (often "done"); the actual question lives in the
            # full text. One live run forwarded the word "done" to the
            # competence check and escalated a task that needed no owner.
            question = ((getattr(outcome, "text", "") or "").strip()
                        or outcome.detail)
            return answer_question(self.repo_root, self.cfg, self.meta,
                                   question, self.journal, task.name)
        except Exception as e:
            self.journal.event("question handling failed: %s" % type(e).__name__)
            return None

    def _say(self, text):
        if self.verbose:
            print(text)
