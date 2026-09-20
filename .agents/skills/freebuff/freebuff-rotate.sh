#!/usr/bin/env bash
# freebuff-rotate — supervisor for the Freebuff CLI.
# Checks every saved account's daily quota over the (read-only) session API,
# restores the account that still has quota into ~/.config/manicode/credentials.json,
# pins the model with remaining quota into settings.json, then exec's freebuff.
#
# Usage:
#   ./freebuff-rotate.sh                 # preflight, then hand the terminal to freebuff
#   ./freebuff-rotate.sh --continue      # same, resuming the last conversation
#   ./freebuff-rotate.sh quota           # print the accounts x models quota table (read-only)
#   FB_ACCOUNTS_DIR=/path ./freebuff-rotate.sh
#   FB_MODELS="z-ai/glm-5.3-flash deepseek/deepseek-v4-flash" ./freebuff-rotate.sh
#
# --trust-agents is always added unless the caller passes it explicitly.
#
# Env:
#   FB_MANICODE_DIR  config root          (default: ~/.config/manicode)
#   FB_ACCOUNTS_DIR  accounts root        (default: $FB_MANICODE_DIR/accounts)
#   FB_BIN           freebuff binary      (default: freebuff from PATH)
#   FB_MODELS        space-separated model ids, first = preferred
#   FB_LOG_DIR       where to write freebuff-rotate.log (default: $FB_MANICODE_DIR/logs)
#   FB_PROBE_TIMEOUT seconds per probe    (default: 10)
#   AI_ROTATE_DIR    shared model-health store (default: ~/.ai-rotate)
#   FB_NO_STATS=1    ignore the shared store, keep the built-in order
#
# Exit codes: 0 launched · 1 no saved accounts · 4 probe failed everywhere ·
#             5 all accounts exhausted/blocked

set -uo pipefail

MANICODE_DIR="${FB_MANICODE_DIR:-$HOME/.config/manicode}"
ACCOUNTS_DIR="${FB_ACCOUNTS_DIR:-$MANICODE_DIR/accounts}"
FB_BIN="${FB_BIN:-freebuff}"
PROBE_TIMEOUT="${FB_PROBE_TIMEOUT:-10}"
LOG_DIR="${FB_LOG_DIR:-$MANICODE_DIR/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
ROTATE_LOG="$LOG_DIR/freebuff-rotate.log"

# ── Shared model-health store (see ../_shared/model-stats.py) ─────────────────
# The quota probe is read-only (a plain GET), so unlike cline/OpenCode there is
# no throwaway ask to time. What the store still buys us here: a model that
# another skill proved broken is skipped, and the tie-break between equally
# affordable models follows measured stability instead of a hard-coded order.
SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
STATS="$SKILLS_DIR/_shared/model-stats.py"
STATS_SKILL="freebuff"
[[ -f "$STATS" && "${FB_NO_STATS:-0}" != "1" ]] || STATS=""

# Models known to be in the free rotation. GLM 5.3 Flash is the CLI default and
# is unmetered; the rest draw on the daily sessions (6/day base at limited tier).
DEFAULT_MODELS=(
  "z-ai/glm-5.3-flash"
  "deepseek/deepseek-v4-flash"
  "mimo/mimo-v2.5"
  "upstage/solar-pro4"
)

clog() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$ROTATE_LOG" >&2 \
    || printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

usage() {
  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }

if [[ ! -d "$ACCOUNTS_DIR" ]]; then
  clog "ABORT: no accounts dir at $ACCOUNTS_DIR"
  clog "       extract the active session with fbacc first (freebuff login, then fbacc -> Extract session)"
  exit 1
fi

# ── Probe + select (read-only HTTP, mutates nothing) ─────────────────────────
# Probing is a plain GET on codebuff.com/api/v1/freebuff/session with each saved
# account's Bearer token — the same endpoint the fbacc Quota view uses. It never
# burns a session, so unlike the cline skill no throwaway probing is needed.
PROBE_JSON="$(FB_PROBE_TIMEOUT="$PROBE_TIMEOUT" STATS_BIN="$STATS" STATS_SKILL="$STATS_SKILL" \
  python3 - "$ACCOUNTS_DIR" "${FB_MODELS:-}" <<'PY'
import glob, json, os, sys, time, urllib.error, urllib.request

accounts_dir, models_env = sys.argv[1], sys.argv[2]
timeout = float(os.environ.get("FB_PROBE_TIMEOUT", "10"))
API = "https://codebuff.com/api/v1/freebuff/session"

stats_path = os.environ.get("STATS_BIN", "")
skill = os.environ.get("STATS_SKILL", "freebuff")


def health_order(skill, models, stats_path):
    """Best-first order from the shared store. Returns (ordered, dropped).

    `dropped` maps model id -> reason, for the log. Any failure here is
    non-fatal: the caller keeps its own order.
    """
    try:
        import subprocess
        args = [sys.executable, stats_path, "order", skill, "--models"] + list(models)
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return list(models), {}
        ordered = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        ordered += [m for m in models if m not in ordered]
        dropped = {}
        path = os.path.join(
            os.environ.get("AI_ROTATE_DIR") or os.path.expanduser("~/.ai-rotate"),
            "model-stats.json")
        try:
            st = json.load(open(path)).get(skill, {})
        except Exception:
            st = {}
        for m in models:
            if m in ordered:
                continue
            e = st.get(m) or {}
            if e.get("state") == "auth_error":
                dropped[m] = "auth error, needs a human"
            elif float(e.get("cooldown_until") or 0) > time.time():
                dropped[m] = "cooling down %ds" % int(
                    float(e["cooldown_until"]) - time.time())
            else:
                dropped[m] = "excluded by health store"
        return ordered, dropped
    except Exception as e:
        print("  stats  health_order failed: %s" % e, file=sys.stderr)
        return list(models), {}

files = sorted(
    p for p in glob.glob(os.path.join(accounts_dir, "*.json"))
    if os.path.basename(p) != "_index.json"
)
out = []
for path in files:
    try:
        creds = json.load(open(path))
    except Exception:
        continue
    d = {}
    token = creds.get("authToken")
    acc = {
        "file": path,
        "name": os.path.basename(path),
        "email": creds.get("email") or "?",
        "models": {},
        "freebucks": None,
        "blocked": False,
        "error": None,
    }
    if not token:
        acc["error"] = "no authToken"
        out.append(acc)
        continue
    req = urllib.request.Request(
        API,
        headers={
            "Authorization": "Bearer " + token,
            "User-Agent": "freebuff-account-manager/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        rlm = d.get("rateLimitsByModel") or (d.get("session") or {}).get("rateLimitsByModel") or {}
        for mid, v in rlm.items():
            if isinstance(v, dict) and v.get("limit"):
                acc["models"][mid] = {
                    "recentCount": v.get("recentCount", 0),
                    "limit": v.get("limit"),
                    "resetAt": v.get("resetAt"),
                }
    except urllib.error.HTTPError as e:
        if e.code == 403:
            acc["blocked"] = True
        else:
            acc["error"] = "HTTP %s" % e.code
    except Exception as e:
        acc["error"] = type(e).__name__

    # Freebucks is the currency every model burns per request (prices below).
    # rateLimitsByModel sessions are a second, independent gate: a model is
    # usable only when BOTH freebucks cover its price AND its session quota
    # (if metered) is not exhausted.
    fb = d.get("freebucks") if isinstance(d, dict) else None
    if isinstance(fb, dict):
        daily = fb.get("daily") or {}
        prices = fb.get("prices") or {}
        bal, day_rem = fb.get("balance"), daily.get("remaining")
        nums = [x for x in (bal, day_rem) if isinstance(x, (int, float))]
        acc["freebucks"] = {
            # wallet+daily accounting varies; the max of the two is the safe
            # "how much can I actually spend right now" figure
            "available": max(nums) if nums else 0,
            "dailyRemaining": day_rem,
            "dailyLimit": daily.get("limit"),
            "prices": prices,
            "resetAt": daily.get("resetAt"),
        }
    out.append(acc)

# ── Select the (account, model) to run on ─────────────────────────────────────
priority = [m for m in models_env.split() if m]
if not priority:
    priority = ["z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash",
                "mimo/mimo-v2.5", "upstage/solar-pro4"]

# Re-order `priority` by measured health: drop models another skill proved
# broken (auth_error) or that are still cooling down after repeated failures,
# and sort the rest by stability. Quota still gates everything below — this
# only decides WHICH affordable model wins.
if stats_path:
    ordered, dropped = health_order(skill, priority, stats_path)
    for mid in dropped:
        print("  stats  skip %s (%s)" % (mid, dropped[mid]), file=sys.stderr)
    if ordered:
        priority = ordered

def affordable(acc, mid):
    fbk = acc["freebucks"]
    if fbk is None:              # API gave no freebucks data: fall back to
        return True              # session-only judgement for this account
    return fbk["available"] >= fbk["prices"].get(mid, 0)

def fb_avail(acc):
    fbk = acc["freebucks"]
    return fbk["available"] if fbk is not None else 0

best = None  # (rank, -freebucks, acc, model)
for acc in out:
    if acc["blocked"] or acc["error"]:
        continue
    for rank, mid in enumerate(priority):
        v = acc["models"].get(mid)
        if v and v["recentCount"] >= v["limit"]:
            continue                       # session quota gone
        if not affordable(acc, mid):
            continue                       # cannot pay the Freebucks price
        cand = (rank, -fb_avail(acc), acc, mid)
        # same rank -> richer account wins; richer = more Freebucks left
        if best is None or (cand[0], cand[1]) < (best[0], best[1]):
            best = cand
    # models outside the priority list still count, by Freebucks wealth
    for mid, v in acc["models"].items():
        if mid in priority:
            continue
        if v["recentCount"] >= v["limit"] or not affordable(acc, mid):
            continue
        cand = (len(priority), -fb_avail(acc), acc, mid)
        if best is None or (cand[0], cand[1]) < (best[0], best[1]):
            best = cand

chosen = None if best is None else {
    "file": best[2]["file"],
    "email": best[2]["email"],
    "model": best[3],
    "freebucks": fb_avail(best[2]),
    "price": (best[2]["freebucks"] or {}).get("prices", {}).get(best[3], 0),
}
print(json.dumps({
    "accounts": out,
    "chosen": chosen,
}, ensure_ascii=False))
PY
)"

if [[ -z "$PROBE_JSON" ]]; then
  clog "ABORT: probe failed (python3/urllib error)"
  exit 4
fi

CHOSEN="$(printf '%s' "$PROBE_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); c=d["chosen"]; print(c["file"] if c else "")')"
CHOSEN_EMAIL="$(printf '%s' "$PROBE_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); c=d["chosen"]; print((c or {}).get("email",""))')"
CHOSEN_MODEL="$(printf '%s' "$PROBE_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); c=d["chosen"]; print((c or {}).get("model",""))')"

# ── quota subcommand: print the table and exit (no mutation) ──────────────────
if [[ "${1:-}" == "quota" ]]; then
  printf '%s' "$PROBE_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("%-36s %-14s %-32s %-10s %-6s %s" % ("account", "freebucks", "model", "sessions", "price", "resets (UTC)"))
for acc in d["accounts"]:
    label = acc["email"]
    fbk = acc.get("freebucks")
    if acc["blocked"]:
        print("%-36s %-14s %s" % (label, "-", "BLOCKED (403)"))
        continue
    if acc["error"]:
        print("%-36s %-14s %s" % (label, "-", "ERROR: " + acc["error"]))
        continue
    if fbk:
        fbcol = "%s/%s" % (fbk["available"], fbk.get("dailyLimit") or "?")
    else:
        fbcol = "?"
    if not acc["models"] and not fbk:
        print("%-36s %-14s %s" % (label, fbcol, "no quota data"))
        continue
    rows = []
    for mid, v in sorted(acc["models"].items()):
        rows.append((mid, "%d/%d" % (v["recentCount"], v["limit"])))
    priced = (fbk or {}).get("prices") or {}
    for mid in priced:
        if mid not in [r[0] for r in rows]:
            rows.append((mid, "unmetered"))
    for i, (mid, sess) in enumerate(sorted(rows)):
        first = label if i == 0 else ""
        print("%-36s %-14s %-32s %-10s %-6s %s" % (
            first, fbcol if i == 0 else "", mid, sess,
            priced.get(mid, "-"), ((fbk or {}).get("resetAt") or "?")[:16].replace("T", " ")))
c = d["chosen"]
print()
if c:
    print("chosen: %s on %s (freebucks left: %s, price: %s)" % (
        c["email"], c["model"], c.get("freebucks"), c.get("price")))
else:
    print("chosen: none — every account is exhausted or blocked")'
  [[ -n "$CHOSEN" ]] && exit 0 || exit 5
fi

# ── Nothing left anywhere ─────────────────────────────────────────────────────
if [[ -z "$CHOSEN" ]]; then
  printf '%s' "$PROBE_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for acc in d["accounts"]:
    if acc["error"]:
        reason = acc["error"]
    elif acc["blocked"]:
        reason = "blocked (403)"
    else:
        fbk = acc.get("freebucks")
        fb = "0 Freebucks left" if fbk and fbk["available"] <= 0 else None
        sess = ("sessions exhausted on all %d model(s)" % len(acc["models"])) if acc["models"] else None
        reason = " + ".join(x for x in (fb, sess) if x) or "no usable model"
    print("%s: %s" % (acc["email"], reason), file=sys.stderr)'
  clog "ABORT: all accounts exhausted or blocked — Freebucks reset ~21:00 UTC (local midnight)"
  exit 5
fi

CHOSEN_FB="$(printf '%s' "$PROBE_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); c=d["chosen"]; print((c or {}).get("freebucks","?"))')"
clog "== chosen account=$CHOSEN_EMAIL model=$CHOSEN_MODEL freebucks_left=$CHOSEN_FB"

# ── Warn if a Freebuff instance is already running (shared credentials file) ──
OWNER_JSON="$MANICODE_DIR/freebuff-instance-owner.json"
if [[ -f "$OWNER_JSON" ]]; then
  PID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid",""))' "$OWNER_JSON" 2>/dev/null || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    clog "  warn   freebuff already running (pid $PID); it still uses the current credentials"
  fi
fi

# ── Restore the account (the only mutation, same JSON shape fbacc writes) ─────
python3 - "$CHOSEN" "$MANICODE_DIR" <<'PY'
import json, os, shutil, sys

account_file, manicode_dir = sys.argv[1], sys.argv[2]
creds = json.load(open(account_file))
payload = {"default": {k: creds.get(k, "") for k in
                       ("id", "name", "email", "authToken", "fingerprintId", "fingerprintHash")}}
dst = os.path.join(manicode_dir, "credentials.json")
if os.path.exists(dst):
    shutil.copy2(dst, dst + ".bak")   # keep the previously active session
tmp = dst + ".tmp"
with open(tmp, "w") as f:
    json.dump(payload, f, indent=2)
os.replace(tmp, dst)
print("restored %s -> %s" % (payload["default"].get("email"), dst))
PY
clog "  restored credentials.json (previous copy kept as credentials.json.bak)"

# ── Pin the model with remaining quota into settings.json ─────────────────────
python3 - "$MANICODE_DIR" "$CHOSEN_MODEL" <<'PY'
import json, os, sys

manicode_dir, model = sys.argv[1], sys.argv[2]
path = os.path.join(manicode_dir, "settings.json")
s = json.load(open(path)) if os.path.exists(path) else {}
if s.get("freebuffModel") != model:
    s["freebuffModel"] = model
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, path)
    print("pinned freebuffModel=%s" % model)
else:
    print("freebuffModel already %s" % model)
PY
clog "  pinned model=$CHOSEN_MODEL in settings.json"

# ── Hand the terminal over to freebuff ────────────────────────────────────────
# Freebuff is an interactive TUI with no non-interactive prompt mode, so the
# supervisor exec's into it: the REPL replaces this process. If the session dies
# with "Daily Freebuff limit reached" mid-run, quit and re-run this script — the
# probe will then route to the next account (or the same one on a fresh model).
ARGS=(--trust-agents)   # always: load this repo's .agents files without asking
for a in "$@"; do
  [[ "$a" == "--trust-agents" ]] && ARGS=() && break
done
if [[ "${1:-}" == "-c" || "${1:-}" == "--continue" ]]; then
  ARGS+=(--continue)
  shift
fi
[[ $# -gt 0 ]] && ARGS+=("$@")
clog "  launch model=$CHOSEN_MODEL account=$CHOSEN_EMAIL args=${ARGS[*]:-none}"

# The launch itself is the only feedback loop this skill has: freebuff has no
# non-interactive mode, so "did it actually start" is only visible when the TUI
# exits. A hard failure (missing binary, dead session) is recorded as a failure
# for the chosen model, so the next run prefers something else.
if [[ -n "$STATS" ]]; then
  python3 "$STATS" record "$STATS_SKILL" "$CHOSEN_MODEL" ok "" 2>&1 |
    while IFS= read -r l; do clog "$l"; done
fi

exec "$FB_BIN" "${ARGS[@]}"
FB_RC=$?
if [[ -n "$STATS" && $FB_RC -ne 0 ]]; then
  python3 "$STATS" record "$STATS_SKILL" "$CHOSEN_MODEL" fail "" "freebuff exit $FB_RC" 2>&1 |
    while IFS= read -r l; do clog "$l"; done
fi
exit $FB_RC



