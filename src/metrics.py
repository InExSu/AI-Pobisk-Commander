"""metrics.py — machine-readable events, one JSON object per line.

journal.md is for humans; events.jsonl is for everything else: live watch,
aggregate stats, Prometheus export, post-hoc reports. Written by a single
append, so it can never meaningfully slow a run — and every call is wrapped so
a monitoring failure can never break the supervisor.

Rotation is handled by logrot.py (events.jsonl -> events.1.jsonl ...).
"""

import json
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

RUN_ID = time.strftime("%Y-%m-%dT%H:%M:%S")
HOST = socket.gethostname()


class Metrics:
    """Append-only event sink. All methods are best-effort."""

    def __init__(self, repo_root, cfg):
        self.cfg = cfg
        d = os.path.join(repo_root, (cfg.get("state") or {}).get("dir", ".ai_pobisk"))
        # Never raise: monitoring is best-effort. A repo root that cannot be
        # created (bad permissions, embedded NUL, ...) must not take the run
        # down with it — emit() will simply no-op.
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        self.path = os.path.join(d, "events.jsonl")
        # RUN_ID is per-process, but `status --live` is a DIFFERENT process
        # looking at a run started earlier. Default to the most recent run in
        # the file, so the observer sees what the worker wrote.
        self.run = self._latest_run() or RUN_ID

    def _latest_run(self):
        """Most recent run id in the event file, or None."""
        best, best_ts = None, -1.0
        try:
            if not os.path.exists(self.path):
                return None
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    ts = e.get("ts") or 0
                    if ts > best_ts:
                        best, best_ts = e.get("run"), ts
        except OSError:
            return None
        return best

    def emit(self, etype, **fields):
        """One event = one line. Never raises."""
        try:
            ev = {"ts": time.time(), "iso": _iso(), "run": self.run,
                  "host": HOST, "type": etype}
            ev.update(fields)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── readers ───────────────────────────────────────────────────────────────
    def events(self, run=None):
        """Parsed events, optionally filtered to one run."""
        out = []
        if not os.path.exists(self.path):
            return out
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if run and e.get("run") != run:
                        continue
                    out.append(e)
        except OSError:
            pass
        return out

    def runs(self):
        seen = {}
        for e in self.events():
            seen.setdefault(e.get("run"), []).append(e)
        return seen


def _iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _percentile(vals, p):
    """Nearest-rank percentile; [] -> 0."""
    if not vals:
        return 0
    s = sorted(vals)
    import math
    k = max(0, math.ceil(p / 100.0 * len(s)) - 1)
    return s[min(k, len(s) - 1)]


def summarize(events):
    """Aggregate a run's events into one dict. Pure: no I/O."""
    out = {
        "events": len(events),
        "tasks": {},
        "outcomes": {},
        "skills": {},
        "attempts": 0,
        "retries": 0,
        "nudges": 0,
        "rotations": 0,
        "questions": 0,
        "invariants": 0,
        "step_seconds": [],
        "wall_ms": 0,
    }

    ts = [e.get("ts") or 0 for e in events]
    out["wall_ms"] = int((max(ts) - min(ts)) * 1000) if ts else 0

    for e in events:
        t = e.get("type")
        task = e.get("task")
        if task:
            st = out["tasks"].setdefault(
                task, {"status": "running", "attempts": 0, "retries": 0,
                       "elapsed_ms": 0, "skill": e.get("skill", "")})
            if e.get("skill"):
                st["skill"] = e["skill"]
        if t == "attempt":
            out["attempts"] += 1
            oc = e.get("outcome", "?")
            out["outcomes"][oc] = out["outcomes"].get(oc, 0) + 1
            sk = e.get("skill", "?")
            s = out["skills"].setdefault(sk, {"calls": 0, "ok": 0})
            s["calls"] += 1
            if oc == "ok":
                s["ok"] += 1
            if e.get("elapsed_ms"):
                out["step_seconds"].append(e["elapsed_ms"] / 1000.0)
            if task:
                st = out["tasks"][task]
                st["attempts"] += 1
                st["elapsed_ms"] += e.get("elapsed_ms") or 0
        elif t == "retry":
            out["retries"] += 1
        elif t == "nudge":
            out["nudges"] += 1
        elif t == "rotate":
            out["rotations"] += 1
        elif t == "question":
            out["questions"] += 1
        elif t == "invariant":
            out["invariants"] += 1
        elif t == "task_end":
            if task:
                out["tasks"].setdefault(task, {})["status"] = e.get("result", "?")
            out["step_seconds"].append((e.get("wall_ms") or 0) / 1000.0)

    secs = out["step_seconds"]
    out["p50"] = round(_percentile(secs, 50), 1)
    out["p95"] = round(_percentile(secs, 95), 1)
    for sk, s in out["skills"].items():
        s["ok_pct"] = round(100.0 * s["ok"] / s["calls"], 1) if s["calls"] else 0
    return out


def check_thresholds(summary, cfg):
    """Breached thresholds. Returns a list of human-readable strings."""
    issues = []
    b = cfg.get("budget") or {}
    m = cfg.get("monitor") or {}

    max_fatal = float(m.get("max_fatal_share", 0.3))
    total = summary.get("attempts") or 0
    fatal = (summary["outcomes"].get("fatal", 0)
             + summary["outcomes"].get("loop_guard", 0))
    if total >= 5 and fatal / float(total) > max_fatal:
        issues.append("доля fatal %.0f%% > %.0f%% — вероятна поломка конфигурации"
                      % (100.0 * fatal / total, 100 * max_fatal))

    max_nudges = int(m.get("max_nudges_total", 6))
    if summary.get("nudges", 0) > max_nudges:
        issues.append("подталкиваний %d > %d — модель в петле подтверждений"
                      % (summary["nudges"], max_nudges))

    p95 = summary.get("p95") or 0
    p50 = summary.get("p50") or 0
    if p50 > 0 and p95 > 2 * p50:
        issues.append("p95 %.0fs > 2× медианы %.0fs — деградация провайдера"
                      % (p95, p50))

    step_cap = float(b.get("step_timeout_sec", 1800))
    for task, st in (summary.get("tasks") or {}).items():
        if st.get("elapsed_ms", 0) / 1000.0 > step_cap * 0.8:
            issues.append("задача %s близка к таймауту шага (%.0fs из %.0fs)"
                          % (task, st["elapsed_ms"] / 1000.0, step_cap))
    return issues


def skill_health_prometheus(repo_root, cfg, adapters, health):
    """Expose per-skill health so a dashboard can see parked/cooling models."""
    esc = lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"')
    lines = [
        "# HELP aipobisk_skill_models Number of models tracked for a skill",
        "# TYPE aipobisk_skill_models gauge",
        "# HELP aipobisk_skill_cooling Models in cooldown",
        "# TYPE aipobisk_skill_cooling gauge",
        "# HELP aipobisk_skill_auth_error Models parked on an auth error",
        "# TYPE aipobisk_skill_auth_error gauge",
    ]
    for name in sorted(health):
        h = health[name]
        lines.append('aipobisk_skill_models{skill="%s"} %d'
                     % (esc(name), h["models"]))
        lines.append('aipobisk_skill_cooling{skill="%s"} %d'
                     % (esc(name), h["cooling"]))
        lines.append('aipobisk_skill_auth_error{skill="%s"} %d'
                     % (esc(name), h["auth"]))
    return "\n".join(lines) + "\n"


def prometheus(summary, cfg):
    """Prometheus text exposition format. Enough for a scrape; no deps."""
    lines = []
    esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')

    def gauge(name, value, helptext, labels=None):
        lines.append("# HELP %s %s" % (name, helptext))
        lines.append("# TYPE %s gauge" % name)
        if labels:
            for lv in labels:
                lines.append('%s{%s} %s' % (name, lv, value))
        else:
            lines.append("%s %s" % (name, value))

    gauge("aipobisk_events_total", summary.get("events", 0),
          "Events recorded in this run")
    gauge("aipobisk_attempts_total", summary.get("attempts", 0),
          "Worker attempts in this run")
    gauge("aipobisk_retries_total", summary.get("retries", 0), "Retries")
    gauge("aipobisk_nudges_total", summary.get("nudges", 0),
          "Times ai_Pobisk answered a confirmation prompt for the user")
    gauge("aipobisk_rotations_total", summary.get("rotations", 0),
          "Model/account rotations")
    gauge("aipobisk_questions_total", summary.get("questions", 0),
          "Questions from worker models")
    gauge("aipobisk_invariants_total", summary.get("invariants", 0),
          "Decisions made by hard invariants rather than the meta model")
    gauge("aipobisk_wall_clock_seconds", round(summary.get("wall_ms", 0) / 1000.0, 1),
          "Wall clock of this run")
    gauge("aipobisk_step_seconds", summary.get("p50", 0),
          "Median step duration", ['quantile="0.5"'])
    gauge("aipobisk_step_seconds", summary.get("p95", 0),
          "95th percentile step duration", ['quantile="0.95"'])

    labels, vals = [], []
    for oc, n in sorted((summary.get("outcomes") or {}).items()):
        labels.append('outcome="%s"' % esc(oc))
        vals.append((oc, n))
    if vals:
        lines.append("# HELP aipobisk_outcomes_total Outcomes by type")
        lines.append("# TYPE aipobisk_outcomes_total gauge")
        for oc, n in vals:
            lines.append('aipobisk_outcomes_total{outcome="%s"} %d' % (esc(oc), n))

    lines.append("# HELP aipobisk_task_done 1 if the task finished successfully")
    lines.append("# TYPE aipobisk_task_done gauge")
    for task, st in sorted((summary.get("tasks") or {}).items()):
        done = 1 if st.get("status") == "done" else 0
        lines.append('aipobisk_task_done{task="%s"} %d' % (esc(task), done))

    lines.append("# HELP aipobisk_skill_ok_ratio Share of successful calls")
    lines.append("# TYPE aipobisk_skill_ok_ratio gauge")
    for sk, s in sorted((summary.get("skills") or {}).items()):
        lines.append('aipobisk_skill_ok_ratio{skill="%s"} %s'
                     % (esc(sk), s.get("ok_pct", 0)))
    return "\n".join(lines) + "\n"
