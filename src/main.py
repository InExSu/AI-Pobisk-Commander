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
    mail [--wait SEC] [--code]                read the registration mailbox
    signup URL [--yes] [--dry-run]            an AI fills a registration form
    login URL [--wait SEC]                    open a window for OAuth / 2FA
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


def cmd_repair(repo_root, args):
    """Release model-health parks that were not caused by a broken credential.

    A park bans a model from rotation for a day. The rotate scripts used to
    park a model whenever a probe failed for any reason at all — a provider
    stream error, an empty balance, a timeout — and those false parks quietly
    shrank the fleet until only one model was left. This command releases
    them, keeping the parks whose reason really does name a credential.
    """
    import importlib.util
    shared = os.path.join(repo_root, ".agents", "skills", "_shared",
                          "model-stats.py")
    if not os.path.exists(shared):
        print("model-stats.py not found at %s" % shared, file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("model_stats_repair", shared)
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)
    return ms.cmd_repair([])


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
                          % (name[:26], t.get("skill", "-")[:12],
                             t.get("model", "-")[:22],
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


def cmd_mail(repo_root, args):
    """What is in the registration mailbox, and is the code there yet.

    `mail`                 last letters: subject, sender, code, confirmation link
    `mail --wait 180`      block until a *new* letter with a code arrives
    `mail --code`          print only the code (for scripts)
    `mail --from x --days 2 --lines 5`
    """
    from mailbox import Mail, MailError

    cfg, _ = config.load(repo_root)
    conf = cfg["mail"]
    if not conf.get("host") or not conf.get("user"):
        print("mail: [mail] host/user are not configured in configs/ai_pobisk.toml")
        return 2
    sender = getattr(args, "sender", "") or ""
    if not sender and getattr(args, "sub", "status") not in ("", "status"):
        sender = args.sub
    subject = getattr(args, "subject", "") or ""

    try:
        mail = Mail(conf).connect()
    except MailError as e:
        print("mail: %s" % e)
        return 2

    try:
        if args.wait:
            # Watermark first: an old letter must not pass for a fresh code.
            since = mail.last_uid()
            got = mail.wait_for_code(since_uid=since, timeout=args.wait,
                                     sender=sender or None,
                                     subject=subject or None)
            if not got:
                print("mail: no code within %ss" % args.wait)
                return 3
            msg, code = got
            print("%s" % code if args.code else
                  "%s | %s | code=%s" % (msg.date, msg.sender, code))
            return 0

        msgs = mail.messages(sender=sender or None, subject=subject or None,
                             days=getattr(args, "days", 0) or None,
                             limit=args.lines)
        if not msgs:
            print("mail: nothing found")
            return 1
        hosts = [getattr(args, "links", "")] if getattr(args, "links", "") else []
        for msg in reversed(msgs):
            code = msg.code()
            if args.code:
                if code:
                    print(code)
                continue
            when = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
            print("%s  %s" % (when, msg.subject[:70]))
            print("   from: %s" % msg.sender[:70])
            if code:
                print("   code: %s" % code)
            links = msg.links(hosts, limit=2)
            for link in links:
                print("   link: %s" % link[:110])
            if args.raw:
                print("   " + msg.body[:400].replace("\n", " "))
        return 0
    finally:
        mail.close()


def cmd_login(repo_root, args):
    """Open a site in a real window so the owner can pass OAuth / 2FA.

    Some sites have no password form at all — apinex.bond, for instance, offers
    only "Continue with Google". An agent must not attempt that step: it is the
    owner's identity, Google blocks automated browsers, and a wrong attempt
    burns the account. So the agent opens a visible window, the human signs in,
    and the persistent profile keeps the session for every later command.

    Nothing is typed by the program here. It only waits and then reports.
    """
    import browser
    import pageview

    url = args.sub if str(getattr(args, "sub", "")).startswith("http") else ""
    if not url and args.query:
        url = args.query[0]
    if not url:
        print("usage: ./ai_Pobisk.sh login https://site/keys [--wait 300]")
        return 2
    if not browser.available():
        print(browser.install_hint())
        return 2

    cfg, _ = config.load(repo_root)
    site = url.split("//", 1)[-1].split("/")[0].lower().replace("www.", "")
    driver = browser.PlaywrightDriver(
        os.path.join(repo_root, cfg["state"]["dir"], "browser", site),
        headless=False, channel="chrome")
    driver.start()
    driver.goto(url)
    wait = float(args.wait or 300)
    print("login: window open at %s" % driver.url())
    print("login: sign in there (Google / OAuth / 2FA); waiting up to %.0fs" % wait)

    # Paths that mean "still not authorized". Everything else counts as a
    # session: an SPA that keeps /login after success does not exist, and a
    # false positive here is harmless (the profile is kept either way).
    loginish = ("/login", "/signin", "/sign-in", "/auth", "/oauth",
                "accounts.google.com", "about:blank")
    deadline = time.time() + wait
    try:
        while time.time() < deadline:
            current = driver.url()
            if current and not any(m in current for m in loginish):
                driver.wait(1500)                 # let the SPA settle
                view = pageview.parse(driver.html(), url=driver.url())
                shot = os.path.join(repo_root, cfg["state"]["dir"], "screens",
                                    "login-%s-%s.png"
                                    % (site.replace(".", "-"),
                                       time.strftime("%Y%m%d-%H%M%S")))
                driver.screenshot(shot)
                print("login: authorized — %s" % view.url)
                print("login: page %r" % view.title)
                print("login: profile kept in %s" % driver.profile_dir)
                return 0
            driver.wait(1000)
        print("login: timeout, still at %s" % driver.url())
        return 3
    finally:
        driver.close()


def cmd_signup(repo_root, args):
    """An AI fills a registration form: snapshot -> decide -> act -> repeat.

    Nothing is clicked unless BOTH `allow_live = true` in the config and
    `--yes` on the command line: creating accounts is an external, hard to
    undo action. Without them the run prints the first planned step.
    """
    import browser
    import signup as signup_mod

    url = args.sub if str(getattr(args, "sub", "")).startswith("http") else ""
    if not url and args.query:
        url = args.query[0]
    if not url:
        print("usage: ./ai_Pobisk.sh signup https://site/register [--yes] [--dry-run]")
        return 2
    cfg, adapters = build(repo_root)
    conf = cfg["signup"]
    profile_path = getattr(args, "profile", "") or os.path.join(
        repo_root, conf.get("profile") or "configs/profile.toml")
    if not os.path.isabs(profile_path):
        profile_path = os.path.join(repo_root, profile_path)
    profile = signup_mod.Profile.load(profile_path)
    if not profile.value("email"):
        print("signup: no e-mail in %s — copy configs/profile.example.toml"
              % profile_path)
        return 2
    if not browser.available():
        print(browser.install_hint())
        return 2

    # Two consents, both required: the project-level switch says "this machine
    # may create accounts", --yes says "this particular run may". Either alone
    # is a mistake waiting to happen — a config left on, or a reflex --yes.
    live = bool(args.yes) and bool(conf.get("allow_live"))
    site = signup_mod.site_of(url)
    driver = browser.PlaywrightDriver(
        os.path.join(repo_root, cfg["state"]["dir"], "browser", site),
        headless=bool(conf.get("headless", True)) and not args.headed,
        slow_mo_ms=0)
    mail = None
    if cfg["mail"].get("host"):
        from mailbox import Mail
        mail = Mail(cfg["mail"])

    thinker = signup_mod.Thinker(repo_root, cfg, adapters)
    runner = signup_mod.Signup(repo_root, cfg, driver, thinker=thinker,
                               mail=mail, profile=profile)
    if not live:
        why = ("allow_live = false in [signup]" if not conf.get("allow_live")
               else "no --yes on the command line")
        print("signup: dry run (%s) — nothing is clicked" % why)
    try:
        res = runner.run(url, dry_run=not live, max_steps=args.max_steps,
                         allow_live=live, save_password=args.save_password)
    finally:
        try:
            shot = os.path.join(repo_root, conf.get("screens_dir") or
                                ".ai_pobisk/screens",
                                "%s-%s.png" % (site.replace(".", "-"),
                                               time.strftime("%Y%m%d-%H%M%S")))
            driver.screenshot(shot)
        except Exception:
            pass
        driver.close()
        if mail is not None:
            mail.close()

    print()
    print("status : %s" % res["status"])
    print("site   : %s" % res["site"])
    print("reason : %s" % res["reason"])
    print("steps  : %s   llm calls: %s   model: %s"
          % (res["steps"], res["llm_calls"],
             "%s/%s" % res["model"] if res["model"] else "rules only"))
    if res.get("login"):
        print("login  : %s" % res["login"])
    if res.get("trace"):
        print("trace  : %s" % res["trace"])
    return 0 if res["status"] in ("registered", "dry_run") else 3


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
                             "tail", "drakon", "mail", "signup", "login",
                             "repair"])
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
    # ── mail / signup ─────────────────────────────────────────────────────────
    ap.add_argument("--wait", type=float, default=0.0,
                    help="mail: block up to SEC seconds waiting for a new code")
    ap.add_argument("--code", action="store_true",
                    help="mail: print only the code")
    ap.add_argument("--from", dest="sender", metavar="ADDR", default="",
                    help="mail: only letters whose From contains ADDR")
    ap.add_argument("--subject", metavar="TEXT", default="",
                    help="mail: only letters whose Subject contains TEXT")
    ap.add_argument("--days", type=int, default=0,
                    help="mail: only letters from the last N days")
    ap.add_argument("--links", metavar="HOST", default="",
                    help="mail: only links on HOST (and its subdomains)")
    ap.add_argument("--raw", action="store_true",
                    help="mail: also print the body")
    ap.add_argument("--profile", metavar="PATH", default="",
                    help="signup: who we are (default: [signup].profile)")
    ap.add_argument("--yes", action="store_true",
                    help="signup: really click; without it the run is a dry run")
    ap.add_argument("--headless", action="store_true",
                    help="signup: hide the browser window (the default)")
    ap.add_argument("--headed", action="store_true",
                    help="signup: show the window (captcha / debugging)")
    ap.add_argument("--save-password", dest="save_password", action="store_true",
                    help="signup: store the generated password in ~/.ai-rotate")
    ap.add_argument("--max-steps", dest="max_steps", type=int, default=0,
                    help="signup: step cap (default: [signup].max_steps)")
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
          "drakon": cmd_drakon, "mail": cmd_mail, "signup": cmd_signup,
          "login": cmd_login, "repair": cmd_repair}[args.command]
    return fn(repo_root, args)


if __name__ == "__main__":
    sys.exit(main())
