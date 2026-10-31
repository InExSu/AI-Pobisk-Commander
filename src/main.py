#!/usr/bin/env python3
"""main.py — ai_Pobisk entry point (invoked by the ./ai_Pobisk.sh wrapper).

Commands:
    run [--task NAME] [--dry-run] [--fresh]   execute tasks from configs/tasks
    list                                      show discovered tasks
    status [--live] [--json]                  state, or aggregates of a run
    skills [--probe]                          adapters, health, optional ping
    self-test                                 meta model + adapters + machine
    watch [--json] [--interval SEC]           live view during a run
    tail [--lines N] [--follow]               last events, like tail -f
    check [--notify-cmd CMD]                  thresholds; exit 1 if breached
    serve [--port N]                          /metrics in Prometheus format
    report [--run ID]                         post-hoc breakdown of a run
    graft [build|ask] [--deep]                code-graph context layer
    drakon [--svg PATH] [check]               the control machine + silhouette
"""

import argparse
import json
import os
import sys
import time
from concurrent import futures

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, os.path.join(_HERE, "adapters"),
          os.path.join(_ROOT, ".agents", "skills", "_shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config          # noqa: E402
import dispatcher      # noqa: E402
import journal as journal_mod   # noqa: E402
import meta as meta_mod          # noqa: E402
import graftctx       # noqa: E402
import metrics        # noqa: E402
import registry        # noqa: E402
import supervisor as sup_mod     # noqa: E402


def build(repo_root, verbose=True):
    cfg, _ = config.load(repo_root)
    adapters = {a.skill: a for a in registry.build(repo_root)}
    return cfg, adapters


def _skill_health(repo_root, cfg, adapters):
    """Per-skill health from model-stats.json + liveness. Pure-ish, no models.

    Returns {skill: {available, role, models, live, cooling, auth, p95, calls}}
    """
    import json as _json
    store = os.path.join(os.environ.get("AI_ROTATE_DIR")
                         or os.path.expanduser("~/.ai-rotate"),
                         "model-stats.json")
    try:
        stats = _json.load(open(store))
    except Exception:
        stats = {}

    out = {}
    non_workers = set(cfg["workers"]["non_workers"])
    for name in cfg["workers"]["order"] + list(non_workers):
        a = adapters.get(name)
        if not a:
            continue
        ent = stats.get(name) or {}
        now = time.time()
        cooling = sum(1 for e in ent.values()
                      if (e.get("cooldown_until") or 0) > now)
        auth = sum(1 for e in ent.values() if e.get("state") == "auth_error")
        live = sum(1 for e in ent.values()
                   if e.get("state") in ("healthy", "recovering"))
        calls = sum((e.get("uptime") or [0, 0])[1] for e in ent.values())
        p95s = [e.get("p95") or 0 for e in ent.values()
                if (e.get("uptime") or [0, 0])[0]]
        out[name] = {
            "available": a.available(),
            "role": "capacity probe" if name in non_workers else "worker",
            "models": len(ent),
            "live": live,
            "cooling": cooling,
            "auth": auth,
            "calls": calls,
            "p95": round(max(p95s), 1) if p95s else 0,
        }
    return out


def cmd_skills(repo_root, args):
    cfg, adapters = build(repo_root)
    probe = getattr(args, "probe", False)
    health = _skill_health(repo_root, cfg, adapters)

    print("%-12s %-9s %-9s %-7s %-8s %-5s %s"
          % ("skill", "available", "role", "models", "live/cool", "calls", "p95"))
    for name, h in health.items():
        print("%-12s %-9s %-9s %-7d %-8s %-5d %s"
              % (name,
                 "yes" if h["available"] else "NO",
                 h["role"],
                 h["models"],
                 "%d/%d" % (h["live"], h["cooling"]),
                 h["calls"],
                 ("%.0fms" % h["p95"]) if h["p95"] else "-"))
        if h["auth"]:
            print("%-12s   %d model(s) parked: auth error — needs a human"
                  % ("", h["auth"]))

    if not probe:
        print()
        print("(add --probe to actually ping each model — costs one ask per model)")
        return 0

    # --probe: real calls. Deliberately opt-in: it spends quota.
    print()
    print("probing (one ask per model)...")
    ok = 0
    total = 0
    for name, a in adapters.items():
        if not a.available():
            continue
        for m in (a.models() if hasattr(a, "models") else []):
            total += 1
            o = a.run("Reply with exactly: ok", model=m,
                      timeout=int(cfg["meta"]["timeout_sec"]))
            flag = "OK  " if o.outcome == "ok" else o.outcome.upper()
            print("  %-12s %-46s %-14s %sms"
                  % (name, m[:46], flag, o.elapsed_ms))
            if o.outcome == "ok":
                ok += 1
    print()
    print("%d/%d alive" % (ok, total))
    return 0 if ok else 1


def cmd_list(repo_root, args):
    cfg, _ = build(repo_root)
    tasks, skipped = dispatcher.load_tasks(repo_root, cfg)
    print(dispatcher.describe(tasks, skipped) or "no tasks found")
    return 0


def cmd_status(repo_root, args):
    cfg, _ = build(repo_root)
    live = getattr(args, "live", False)
    as_json = getattr(args, "json", False)
    m = metrics.Metrics(repo_root, cfg)

    if not live:
        j = journal_mod.Journal(repo_root, cfg)
        st = j.state
        if as_json:
            # Same shape as the human view, so scripts do not scrape text.
            print(json.dumps({
                "state_file": j.state_path,
                "updated": st.get("updated"),
                "tasks": st.get("tasks") or {},
            }, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        print("state: %s" % j.state_path)
        if not st.get("tasks"):
            print("  no tasks recorded yet")
            return 0
        for name, t in sorted(st["tasks"].items()):
            print("  %-28s %-10s step=%s attempts=%s"
                  % (name, t.get("status", "?"), t.get("step", 0),
                     t.get("attempts", {})))
            for q in t.get("pending_questions", []):
                print("      PENDING: %s" % q["question"][:80])
        return 0

    # --live: aggregate the current run's event stream.
    ev = m.events(run=m.run)
    summ = metrics.summarize(ev)
    if as_json:
        print(json.dumps(summ, ensure_ascii=False, indent=2))
        return 0

    done = sum(1 for t in summ["tasks"].values() if t.get("status") == "done")
    print("run    %s   %d task(s), %d done" % (m.run, len(summ["tasks"]), done))
    print("wall   %.1fm   events %d   attempts %d"
          % (summ["wall_ms"] / 60000.0, summ["events"], summ["attempts"]))
    print("help   retries %d · rotations %d · nudges %d · questions %d · "
          "invariants %d"
          % (summ["retries"], summ["rotations"], summ["nudges"],
             summ["questions"], summ["invariants"]))
    if summ["outcomes"]:
        print("outcomes " + " · ".join("%s %d" % (k, v)
                                       for k, v in sorted(summ["outcomes"].items())))
    if summ["p50"]:
        print("step   p50 %.0fs · p95 %.0fs" % (summ["p50"], summ["p95"]))
    if summ["skills"]:
        print("skills " + " · ".join(
            "%s %d calls %s%% ok" % (k, v["calls"], v["ok_pct"])
            for k, v in sorted(summ["skills"].items())))
    for name, t in sorted(summ["tasks"].items()):
        print("  %-28s %-10s %s  %.0fs"
              % (name, t.get("status", "running"), t.get("skill", ""),
                 t.get("elapsed_ms", 0) / 1000.0))
    return 0


def cmd_watch(repo_root, args):
    """Live view of a running (or finished) run. Read-only."""
    cfg, _ = build(repo_root)
    m = metrics.Metrics(repo_root, cfg)
    period = float(getattr(args, "interval", 5) or 5)
    as_json = getattr(args, "json", False)
    try:
        while True:
            summ = metrics.summarize(m.events(run=m.run))
            if as_json:
                # One JSON document per tick: pipe it into jq, a widget, or a
                # bot. No screen clearing, no ANSI.
                summ["run"] = m.run
                summ["issues"] = metrics.check_thresholds(summ, cfg)
                print(json.dumps(summ, ensure_ascii=False))
                sys.stdout.flush()
            else:
                os.system("clear")
                print("ai_Pobisk — run %s   (ctrl-c to stop watching)" % m.run)
                print()
                print("%-26s %-12s %-22s %-10s %s"
                      % ("task", "skill", "model", "status", "elapsed"))
                for name, t in sorted(summ["tasks"].items()):
                    print("%-26s %-12s %-22s %-10s %.0fs"
                          % (name[:26], t.get("skill", "-")[:12], "-",
                             t.get("status", "running"), t.get("elapsed_ms", 0) / 1000.0))
                print()
                print("attempts %d · retries %d · nudges %d · rotations %d"
                      % (summ["attempts"], summ["retries"], summ["nudges"],
                         summ["rotations"]))
                for line in metrics.check_thresholds(summ, cfg):
                    print("  !! %s" % line)
            time.sleep(period)
    except KeyboardInterrupt:
        if not as_json:
            print()
        return 0


def cmd_tail(repo_root, args):
    """Last events, optionally following — the `tail -f` of a run."""
    cfg, _ = build(repo_root)
    m = metrics.Metrics(repo_root, cfg)
    n = int(getattr(args, "lines", 20) or 20)
    follow = getattr(args, "follow", False)
    as_json = getattr(args, "json", False)

    def render(ev):
        if as_json:
            return json.dumps(ev, ensure_ascii=False)
        ts = time.strftime("%H:%M:%S", time.localtime(ev.get("ts") or 0))
        bits = [ts, "%-11s" % (ev.get("type") or "?")]
        if ev.get("task"):
            bits.append("%-22s" % str(ev["task"])[:22])
        if ev.get("skill"):
            bits.append("%-10s" % ev["skill"])
        if ev.get("outcome"):
            bits.append("%-13s" % ev["outcome"])
        bits.append(str(ev.get("detail") or "")[:100])
        return " ".join(bits)

    shown = 0
    for ev in m.events(run=m.run)[-n:]:
        print(render(ev))
        shown += 1
    if not shown:
        print("no events for run %s" % m.run)
        return 0
    if not follow:
        return 0

    # Follow: remember how many lines we already printed and poll the file.
    # Cheap (one stat per tick), and it survives log rotation because the
    # reader re-counts rather than holding a file offset.
    seen = len(m.events(run=m.run))
    try:
        while True:
            time.sleep(1.0)
            evs = m.events(run=m.run)
            if len(evs) > seen:
                for ev in evs[seen:]:
                    print(render(ev))
                sys.stdout.flush()
                seen = len(evs)
    except KeyboardInterrupt:
        return 0


def cmd_check(repo_root, args):
    """Threshold check. Exit 1 when something needs attention."""
    cfg, _ = build(repo_root)
    m = metrics.Metrics(repo_root, cfg)
    summ = metrics.summarize(m.events(run=m.run))
    issues = metrics.check_thresholds(summ, cfg)

    # Skill health: parked (auth_error) models silently collapse rotation
    # depth — nobody notices until every model is gone.
    cfg2, adapters = build(repo_root)
    mon = cfg.get("monitor") or {}
    max_auth = int(mon.get("max_auth_error_models", 8))
    for name, h in sorted(_skill_health(repo_root, cfg2, adapters).items()):
        if h["auth"] > max_auth:
            issues.append("скилл %s: запарковано %d моделей по auth error — "
                          "проверить ключи" % (name, h["auth"]))
        if h["models"] and h["live"] == 0:
            issues.append("скилл %s: нет живых моделей (все в cooldown или "
                          "запаркованы)" % name)

    cmd = getattr(args, "notify_cmd", None) or mon.get("notify_cmd") or ""
    if not issues:
        print("ok — no thresholds breached")
        return 0
    print("%d issue(s):" % len(issues))
    for i in issues:
        print("  !! %s" % i)
    if cmd:
        import subprocess
        try:
            subprocess.run(cmd, shell=True, timeout=60,
                           input="\n".join(issues), text=True)
        except Exception:
            pass
    return 1


def cmd_serve(repo_root, args):
    """Prometheus-compatible /metrics on stdlib http.server. No deps."""
    import http.server
    cfg, adapters = build(repo_root)
    m = metrics.Metrics(repo_root, cfg)
    # `0` is a valid request ("pick a free port"), so it must not be treated
    # as "missing" — hence an explicit None check, not `or 9099`.
    port = getattr(args, "port", None)
    port = 9099 if port is None else int(port)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/metrics"):
                summ = metrics.summarize(m.events(run=m.run))
                body = metrics.prometheus(summ, cfg).encode("utf-8")
                try:
                    body += metrics.skill_health_prometheus(
                        repo_root, cfg, adapters,
                        _skill_health(repo_root, cfg, adapters)).encode("utf-8")
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    # --port 0 asks the OS for a free port; report the one we actually got,
    # otherwise the caller has no way to find it.
    port = srv.server_address[1]
    print("metrics on http://127.0.0.1:%d/metrics  (ctrl-c to stop)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_report(repo_root, args):
    """Post-hoc breakdown of a run."""
    cfg, _ = build(repo_root)
    m = metrics.Metrics(repo_root, cfg)
    runs = m.runs()
    if not runs:
        print("no events recorded yet")
        return 0
    want = getattr(args, "run_id", None)
    if want:
        ev = m.events(run=want)
        if not ev:
            print("no such run: %s" % want)
            return 1
    else:
        want = sorted(runs)[-1]
        ev = runs[want]
    summ = metrics.summarize(ev)

    print("run %s" % want)
    print("wall %.1fm · attempts %d · events %d"
          % (summ["wall_ms"] / 60000.0, summ["attempts"], summ["events"]))
    print()
    print("%-28s %-10s %-12s %-8s %s"
          % ("task", "result", "skill", "att", "time"))
    for name, t in sorted(summ["tasks"].items(),
                          key=lambda kv: -kv[1].get("elapsed_ms", 0)):
        print("%-28s %-10s %-12s %-8s %.0fs"
              % (name[:28], t.get("status", "?"), t.get("skill", "-")[:12],
                 t.get("attempts", 0), t.get("elapsed_ms", 0) / 1000.0))
    print()
    print("outcomes: " + " · ".join("%s %d" % (k, v)
                                    for k, v in sorted(summ["outcomes"].items())))
    print("skills:   " + " · ".join(
        "%s %d/%d ok" % (k, v["ok"], v["calls"])
        for k, v in sorted(summ["skills"].items())))
    print("meta/invariant split: invariants %d" % summ["invariants"])
    for i in metrics.check_thresholds(summ, cfg):
        print("  !! %s" % i)
    return 0


def cmd_graft(repo_root, args):
    """graft status / build / ask — the code-graph context layer."""
    cfg, _ = build(repo_root)
    target = (cfg["graft"].get("repo") or "").strip() or repo_root
    action = getattr(args, "sub", "status")

    if action == "build":
        deep = getattr(args, "deep", False)
        rc, out, err = graftctx.build(target, deep=deep)
        print((out or err).strip()[-1500:])
        return 0 if rc == 0 else 1

    if action == "ask":
        q = " ".join(getattr(args, "query", []) or [])
        if not q:
            print("usage: ai_Pobisk.sh graft ask <question>", file=sys.stderr)
            return 1
        block = graftctx.context_for(target, q)
        print(block or "(no context — is the graph built? run: graft build)")
        return 0

    # status
    st = graftctx.status(target)
    enabled = bool(cfg["graft"].get("enabled"))
    print("graft CLI:  %s" % ("installed" if st["installed"] else "not on PATH"))
    print("graph here: %s" % ("built" if st["graph"] else "not built"))
    print("repo:       %s" % target)
    print("in prompts: %s" % ("ON" if enabled else "OFF (config [graft] enabled)"))
    if st["installed"] and st["graph"]:
        m = graftctx.repo_map(target)
        if m:
            print()
            print(m[:1200])
    if not st["installed"]:
        print()
        print("The CLI is optional — the run degrades to a plain prompt without it.")
        print("  global: npm install -g @nanonets/graft")
        print("  no install, same version: npx -y @nanonets/graft@0.28.1 <cmd>")
    if enabled and not (st["installed"] and st["graph"]):
        print()
        print("Enabled in config but unusable: every step pays two failed")
        print("subprocess calls for nothing. Set enabled = false, or build.")
        return 1
    return 0


def cmd_drakon(repo_root, args):
    """The control machine, and the DRAKON silhouette drawn from it.

    `drakon`          print the silhouette as text + where the SVG lives
    `drakon --svg P`  write the SVG silhouette to P
    `drakon check`    prove the machine is sound and the picture is honest
    """
    import statemachine as sm

    action = getattr(args, "sub", "status") or "status"
    problems = sm.validate() + sm.silhouette_problems()

    if action == "check":
        if problems:
            print("%d problem(s):" % len(problems))
            for p in problems:
                print("  !! %s" % p)
            return 1
        print("ok — %s" % sm.summary())
        print("   every state reachable, every state can reach a terminal,")
        print("   every branch in the silhouette exists in the table")
        return 0

    out = getattr(args, "svg", None)
    if out:
        path = sm.write_svg(out)
        print("wrote %s" % path)
        return 0

    print(sm.ascii_art())
    print()
    print(sm.summary())
    if problems:
        print()
        for p in problems:
            print("  !! %s" % p)
        return 1
    return 0


def cmd_self_test(repo_root, args):
    cfg, adapters = build(repo_root)
    print("repo: %s" % repo_root)
    print()
    print("skills:")
    for name, a in adapters.items():
        print("  %-14s %s" % (name, "ok" if a.available() else "MISSING"))
    print()
    print("meta model:")
    m = meta_mod.Meta(repo_root, cfg, adapters)
    if m.select(verbose=True):
        print("  selected: %s / %s" % m.model)
    else:
        print("  none available -> %s (ai_Pobisk still runs)"
              % cfg["fallback"]["on_meta_unavailable"])
    print()
    print("control machine:")
    import statemachine as sm
    problems = sm.validate() + sm.silhouette_problems()
    if problems:
        for p in problems:
            print("  !! %s" % p)
    else:
        print("  %s — sound, silhouette matches the table" % sm.summary())
    print()
    print("tasks:")
    tasks, skipped = dispatcher.load_tasks(repo_root, cfg)
    print("  %d task(s), %d skipped" % (len(tasks), len(skipped)))
    return 0 if tasks and not problems else 1


def cmd_run(repo_root, args):
    cfg, adapters = build(repo_root)
    j = journal_mod.Journal(repo_root, cfg)
    if args.fresh:
        j.reset()

    tasks, skipped = dispatcher.load_tasks(repo_root, cfg)
    if args.task:
        tasks = [t for t in tasks if t.name == args.task]
        if not tasks:
            print("no such task: %s" % args.task, file=sys.stderr)
            return 1

    if not tasks:
        print("no tasks in %s" % cfg["tasks"]["dir"], file=sys.stderr)
        return 1

    # Housekeeping: cap the journal and the health store before a long run.
    try:
        import logrot
        for p in logrot.rotate_all(repo_root, cfg):
            print("rotated: %s" % p)
        store = os.path.join(os.environ.get("AI_ROTATE_DIR")
                             or os.path.expanduser("~/.ai-rotate"),
                             "model-stats.json")
        if logrot.trim_stats(store):
            print("trimmed: model-stats.json")
        swept = logrot.sweep_stats(store)
        if swept:
            print("swept %d dead entr(y/ies) from model-stats.json" % swept)
    except Exception as e:
        print("logrot skipped: %s" % type(e).__name__)

    m = meta_mod.Meta(repo_root, cfg, adapters)
    if not args.dry_run:
        print("selecting meta model...")
        m.select(verbose=True)

    j.write_header(tasks, skipped, m.status())
    for s in skipped:
        j.event("skipped %s" % s)

    sv = sup_mod.Supervisor(repo_root, cfg, adapters, m, j, verbose=not args.quiet)

    results = _run_tasks(repo_root, cfg, tasks, sv, args)

    print("=== summary")
    for name in [t.name for t in tasks]:
        print("  %-28s %s" % (name, results.get(name, "?")))
    failed = [n for n, r in results.items() if r not in ("done", "dry-run")]
    return 0 if not failed else 1


def _run_tasks(repo_root, cfg, tasks, sv, args):
    """Run tasks sequentially or in parallel, per [parallel] config.

    Parallelism is across tasks only: each task gets exactly one worker for
    its whole run, so two agents never edit the same task's files.
    """
    par = cfg.get("parallel") or {}
    results = {}

    if args.dry_run or not par.get("enabled"):
        for t in tasks:
            results[t.name] = sv.run_task(t, dry_run=args.dry_run)
            print()
        return results

    workers = [a for a in registry.workers(repo_root)]
    want = int(par.get("max_parallel_tasks", 1) or 1)
    n = max(1, min(want, len(workers), len(tasks)))

    # Every task targets the same repo -> concurrent edits would collide.
    if par.get("same_repo_guard") and len(tasks) > 1:
        dirs = {sup_mod._target_dir_from_task(t) for t in tasks}
        if len(dirs) == 1 and None not in dirs:
            print("parallel: all tasks target the same repo -> running serially")
            n = 1

    if n == 1:
        for t in tasks:
            results[t.name] = sv.run_task(t, dry_run=False)
            print()
        return results

    print("parallel: %d tasks, %d workers -> %d at a time" % (len(tasks), len(workers), n))
    with futures.ThreadPoolExecutor(max_workers=n) as ex:
        futs = {ex.submit(sv.run_task, t, False): t.name for t in tasks}
        for f in futures.as_completed(futs):
            name = futs[f]
            try:
                results[name] = f.result()
            except Exception as e:
                results[name] = "error: %s" % type(e).__name__
                print("  %s -> error: %s" % (name, e))
            print()
    return results


def main():
    ap = argparse.ArgumentParser(
        prog="ai_Pobisk.sh", description="supervisor for CLI AI agents")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "list", "status", "skills", "self-test",
                             "graft", "watch", "check", "serve", "report",
                             "tail", "drakon"])
    ap.add_argument("--task", help="run only this task file name")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, do not call any model")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore saved state and start over")
    ap.add_argument("--quiet", action="store_true")
    # Sub-verb for `graft` (build | ask | status) and `drakon` (check).
    ap.add_argument("sub", nargs="?", default="status")
    ap.add_argument("query", nargs="*", default=[])
    ap.add_argument("--deep", action="store_true",
                    help="graft build --deep (adds LLM summaries, needs a key)")
    ap.add_argument("--svg", metavar="PATH",
                    help="drakon: write the silhouette as SVG to PATH")
    ap.add_argument("--live", action="store_true",
                    help="status: aggregate the current run's events")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="watch refresh seconds")
    ap.add_argument("--lines", type=int, default=20,
                    help="tail: how many events to show")
    ap.add_argument("--follow", action="store_true",
                    help="tail: keep printing new events (like tail -f)")
    ap.add_argument("--port", type=int, default=9099, help="serve port")
    ap.add_argument("--run", dest="run_id", metavar="ID",
                    help="report: which run to break down")
    ap.add_argument("--notify-cmd", dest="notify_cmd", metavar="CMD",
                    help="check: shell command to run when a threshold fires")
    ap.add_argument("--probe", action="store_true",
                    help="skills: really ping each model (spends one ask each)")
    # Legacy spellings from План.md, kept so old notes still work.
    ap.add_argument("--list", dest="flag_list", action="store_true",
                    help="alias for the `list` command")
    ap.add_argument("--self-test", dest="flag_self_test", action="store_true",
                    help="alias for the `self-test` command")
    args = ap.parse_args()

    if args.flag_list:
        args.command = "list"
    if args.flag_self_test:
        args.command = "self-test"

    repo_root = os.environ.get("AI_POBISK_ROOT") or _ROOT
    fn = {"run": cmd_run, "list": cmd_list, "status": cmd_status,
          "skills": cmd_skills, "self-test": cmd_self_test,
          "graft": cmd_graft, "watch": cmd_watch, "check": cmd_check,
          "serve": cmd_serve, "report": cmd_report, "tail": cmd_tail,
          "drakon": cmd_drakon}[args.command]
    return fn(repo_root, args)


if __name__ == "__main__":
    sys.exit(main())
