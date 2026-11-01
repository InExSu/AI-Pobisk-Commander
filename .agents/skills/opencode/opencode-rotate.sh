#!/usr/bin/env bash
# OpenCode account/model rotation for non-interactive `opencode run`.
# Each account = isolated HOME/XDG tree in ~/.opencode-accounts/<name>.
# Usage:
#   opencode-rotate.sh [--list] [--account NAME] [--model ID] [--keep] -- "<prompt>"
#   opencode-rotate.sh [opts]         # no prompt: probe, then open the interactive TUI
#   --list           show accounts and probe each free model (costs 1 ask per model)
#   --account NAME   pin account; otherwise best free account is picked
#   --model ID       pin model id (opencode/...); otherwise first working free model
#   --keep           reuse last working pair without re-probing
#   --cfg DIR        accounts root (default ~/.opencode-accounts)
#   --bin PATH       opencode binary (default: opencode on PATH)
# Env: OC_MODELS, OC_ACCOUNTS_DIR, OC_BIN, OC_PROBE_TIMEOUT,
#      OC_STALL_WINDOW (silence before kill, default 120), OC_STALL_CHECK (30)
#      AI_ROTATE_DIR (shared model-health store, default ~/.ai-rotate)
#      OC_NO_STATS=1  ignore the shared store, keep the built-in order
set -u

LOG_TS() { date '+%Y-%m-%d %H:%M:%S'; }
log()  { printf '[%s] %s\n' "$(LOG_TS)" "$*" >&2; }  # stderr: stdout is captured by $(pick_model)
die()  { printf '[%s] ERROR: %s\n' "$(LOG_TS)" "$*" >&2; exit 1; }

# ── Shared model-health store (see ../_shared/model-stats.py) ─────────────────
# Probe results are shared with cline/FreeBuff, and rotation order follows
# measured stability instead of the built-in order.
SKILLS_DIR="$(cd "$(dirname "$0")/.." >/dev/null 2>&1 && pwd)"
STATS="$SKILLS_DIR/_shared/model-stats.py"
STATS_SKILL="opencode"
[[ -f "$STATS" && "${OC_NO_STATS:-0}" != "1" ]] || STATS=""

# Best-first ordering of the given models. FAMILY_OF, when set, lifts the
# siblings of the model that just died to the front (family-preserving failover).
# Falls back to the caller's order when the store is off or unreadable.
FAMILY_OF=""
rotation_order() { # $@ = model ids
  [ "$#" -gt 0 ] || return 0
  if [ -z "$STATS" ]; then printf '%s\n' "$@"; return 0; fi
  local args=("$STATS_SKILL" --models "$@") out
  [ -n "$FAMILY_OF" ] && args+=(--family-of "$FAMILY_OF")
  out=$(python3 "$STATS" order "${args[@]}" 2>/dev/null)
  if [ -n "$out" ]; then printf '%s\n' "$out"; else printf '%s\n' "$@"; fi
}

stats_record() { # $1=model $2=verdict $3=latency_ms $4=error
  [ -z "$STATS" ] && return 0
  python3 "$STATS" record "$STATS_SKILL" "$1" "$2" "${3:-}" "${4:-}" 2>&1 |
    while IFS= read -r l; do log "$l"; done
}

# Resolve to an ABSOLUTE path before anything else. `command -v opencode`
# returns the bare name (or nothing) when PATH lacks the nvm bin, and the old
# `cd $(dirname) && pwd` then turned it into "$PWD/opencode" — a file that does
# not exist, so every run died with rc 127 "No such file or directory".
BIN="${OC_BIN:-opencode}"
if ! command -v "$BIN" >/dev/null 2>&1; then
  for cand in "$HOME/.nvm/versions/node/v22.20.0/bin/opencode" \
              "$HOME/.nvm/current/bin/opencode" \
              "/opt/homebrew/bin/opencode" "/usr/local/bin/opencode"; do
    [[ -x "$cand" ]] && BIN="$cand" && break
  done
fi
# Actually CAPTURE what `command -v` found — the old code only tested for
# success and left BIN as the bare name, so the absolute-path step below
# resolved it against $PWD and produced a nonexistent file.
if command -v "$BIN" >/dev/null 2>&1; then
  BIN="$(command -v "$BIN")"
else
  die "opencode binary not found (set OC_BIN=/path/to/opencode)"
fi
BIN="$(cd "$(dirname "$BIN")" >/dev/null 2>&1 && pwd)/$(basename "$BIN")"
[[ -x "$BIN" ]] || die "opencode not executable: $BIN"

# Directory holding the opencode/node binaries, resolved here (where $HOME is
# still set) and passed into run_isolated's clean env.
OC_NODE_BIN="$(dirname "$BIN")"
for cand in "$HOME/.nvm/versions/node/v22.20.0/bin" "$HOME/.nvm/current/bin" \
            "/opt/homebrew/bin" "/usr/local/bin"; do
  [[ -d "$cand" ]] && OC_NODE_BIN="$OC_NODE_BIN:$cand"
done

LIST=0; PIN_ACCOUNT=""; PIN_MODEL=""; KEEP=0; CFG="${OC_ACCOUNTS_DIR:-$HOME/.opencode-accounts}"
PROMPT=""
ADD_NAME=""; ADD_KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) LIST=1 ;;
    --account) PIN_ACCOUNT="${2:-}"; shift ;;
    --model) PIN_MODEL="${2:-}"; shift ;;
    --keep) KEEP=1 ;;
    --cfg) CFG="${2:-}"; shift ;;
    --bin) BIN="${2:-}"; shift ;;
    --add-account) ADD_NAME="${2:-}"; ADD_KEY="${3:-}"; shift 2 ;;
    --) shift; PROMPT="$*"; break ;;
    *) PROMPT="${PROMPT:+$PROMPT }$1" ;;
  esac
  shift
done

# subcommand: register an account (dispatched below, after function definitions)

# Free tier rotates its catalogue: `opencode models --standalone | grep free`
# is the source of truth (re-probed 2026-10-31). The previous list —
# mimo-v2.5-free, ling-3.0-flash-fin-free, nemotron-3-ultra-free, jev-1.13-free
# — no longer exists and made every probe fail with "Model unavailable".
FREE_MODELS_DEFAULT="opencode/beam-free opencode/deepseek-v4.1-flash-free \
opencode/flint-40b-free opencode/glm-5.3-flash-free \
opencode/mimo-v2.6-flash-free opencode/nemotron-3.5-lightning-free \
opencode/nemotron-3.5-super-vl-free"
MODELS="${OC_MODELS:-$FREE_MODELS_DEFAULT}"
PROBE_TIMEOUT="${OC_PROBE_TIMEOUT:-25}"

# Fixed error texts of a dead pair (from the binary + live probes, see SKILL.md)
DEAD_PATTERNS='FreeUsageLimitError|GoUsageLimitError|Free limit reached|Usage limit reached|Rate limit exceeded|[Rr]ate limit|Quota exceeded|No payment method|payment method here|Model is disabled|Model not found|Invalid API key|not authenticated|Unauthorized|FreeTierError|CreditsError'
RETRYABLE_RE='Internal server error|statusCode.*5[0-9][0-9]|isRetryable'
# Credential failures: retrying never fixes them, so the circuit breaker parks
# the model instead of counting it as a transient failure.
# "API key has expired" is the one opencode prints when the key itself is dead —
# without it the run burns its whole retry budget on an account that cannot work.
#
# ONLY a broken credential belongs here. Billing ("No payment method") and quota
# exhaustion are the ACCOUNT's wallet, not the model's character: parking the
# model for them threw away working models and shrank the fleet to nothing.
AUTH_PATTERNS='Invalid API key|API key has expired|key has expired|not authenticated|Unauthorized'
# The account's wallet is empty or its quota is spent. The model is fine — it
# just cannot be used with this account right now. Park it briefly (the
# `limit` state), not for a day.
QUOTA_PATTERNS='No payment method|payment method here|Quota exceeded|FreeUsageLimitError|GoUsageLimitError|Free limit reached|Usage limit reached|FreeTierError|CreditsError'

run_isolated() { # $1=acct_dir, rest=argv — run opencode with full isolation
  local d="$1"; shift
  env -i \
    HOME="$d/home" \
    OPENCODE_TEST_HOME="$d/home" \
    XDG_DATA_HOME="$d/data" XDG_CONFIG_HOME="$d/config" \
    XDG_STATE_HOME="$d/state" XDG_CACHE_HOME="$d/cache" \
    OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_AUTOUPDATE_CHECK=1 \
    # NOTE: $HOME is EMPTY inside `env -i`, so $HOME/... in PATH silently
    # collapses to "/.nvm/versions/..." and the binary is not found (rc 127,
    # "No such file or directory"). Expand the real home before env -i runs.
    PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:${OC_NODE_BIN}" \
    TERM="${TERM:-dumb}" \
    "$BIN" "$@"
}

# opencode has no --timeout flag, so the bound is a heartbeat: every
# STALL_CHECK seconds look at the newest file under the isolated data dir and
# at the captured output. Nothing moved for STALL_WINDOW -> the model is mute.
PROBE_TIMEOUT="${OC_PROBE_TIMEOUT:-45}"
STALL_WINDOW="${OC_STALL_WINDOW:-120}"
STALL_CHECK="${OC_STALL_CHECK:-30}"

mtime_of() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

# CPU ticks (utime+stime) a process has burned, straight from /proc. 0 if
# unknown. `ps` is unavailable in some sandboxes, so read /proc directly. A
# worker running a long test suite burns CPU while writing nothing to its data
# dir — that is the activity the file mtime alone misses, and the one that used
# to get productive workers killed for "silence".
cpu_of() {
  local s
  s="$(cut -d' ' -f14,15 "/proc/$1/stat" 2>/dev/null)"
  [ -z "$s" ] && { echo 0; return; }
  set -- $s
  echo $(( ${1:-0} + ${2:-0} ))
}

# Liveness beacon for an outside observer (see cline-rotate.sh for the why).
# No-op when PB_STATUS_DIR is unset (backward compatible).
_heartbeat() { # $1=pid $2=model $3=state
  [ -z "${PB_STATUS_DIR:-}" ] && return 0
  local hb="$PB_STATUS_DIR/worker_heartbeat.json" now
  now="$(date +%s)"
  printf '{"pid":%s,"model":"%s","state":"%s","last_active":%s}\n' \
    "$1" "$2" "$3" "$now" >"$hb.tmp" 2>/dev/null \
    && mv -f "$hb.tmp" "$hb" 2>/dev/null
}

newest_under() { # $1=dir -> mtime of the most recently written file inside
  local newest=0 f m
  while IFS= read -r f; do
    m="$(mtime_of "$f")"
    [ "$m" -gt "$newest" ] && newest="$m"
  done < <(find "$1" -type f -maxdepth 5 2>/dev/null | head -200)
  echo "$newest"
}

# Watch one run: every STALL_CHECK seconds look at the newest file under the
# isolated data dir, the captured output, OR the worker's CPU ticks. The model
# is mute only when ALL of these have been flat for STALL_WINDOW seconds; a
# worker that is merely quiet on disk (running a long test suite) but burning
# CPU is left alone.
stall_guard() { # $1=pid $2=data_dir $3=out_file
  local pid="$1" data="$2" out="$3" m mo now tick=0 last_cpu last_m=0 last_active cur_cpu
  last_active="$(date +%s)"
  last_cpu="$(cpu_of "$pid")"
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    tick=$((tick + 1))
    [ "$tick" -lt "$STALL_CHECK" ] && continue
    tick=0
    m="$(newest_under "$data")"
    [ -f "$out" ] && { mo="$(mtime_of "$out")"; [ "$mo" -gt "$m" ] && m="$mo"; }
    cur_cpu="$(cpu_of "$pid")"
    # Activity = the worker wrote OR burned CPU this interval. Either means it
    # is alive; only when both are flat for STALL_WINDOW do we kill it.
    if [ "$m" -gt "$last_m" ] || [ "$cur_cpu" -gt "$last_cpu" ]; then
      last_active="$(date +%s)"
      last_m="$m"
      last_cpu="$cur_cpu"
      _heartbeat "$pid" "$MODEL" "active"
      continue
    fi
    now="$(date +%s)"
    if [ $((now - last_active)) -ge "$STALL_WINDOW" ]; then
      log "stall ${STALL_WINDOW}s without activity (files + CPU) -> kill $pid"
      kill -TERM "$pid" 2>/dev/null
      sleep 5
      kill -9 "$pid" 2>/dev/null
      _heartbeat "$pid" "$MODEL" "stalled"
      return 0
    fi
  done
}

# A probe has no output until it finishes, so a stall cannot be detected —
# only a deadline. Without one, one mute model froze the whole rotation for
# minutes (seen live: a probe sat from 21:50 to 21:56 and the supervisor saw
# nothing). PROBE_TIMEOUT is that deadline; the probe is killed, the model is
# counted dead, rotation moves on.
probe_guard() { # $1=pid $2=seconds
  local pid="$1" window="$2" tick=0
  _heartbeat "$pid" "${MODEL:-}" "probing"
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    tick=$((tick + 1))
    [ "$tick" -lt "$window" ] && continue
    kill -TERM "$pid" 2>/dev/null
    sleep 3
    kill -9 "$pid" 2>/dev/null
    return 0
  done
}

check_pair() { # $1=acct_dir $2=model_id -> prints answer, rc=0 if alive
  local out tmp pid gp rc
  # --standalone: a private server per run instead of the shared background
  # service. The shared one keeps a fixed port (49374); a stale instance, or
  # two probes at once, makes every model look dead with "port already in use"
  # — and the service also shells out to `ps`, which sandboxed hosts deny.
  tmp="$(mktemp "${TMPDIR:-/tmp}/oc-probe.XXXXXX")" || tmp=/dev/null
  run_isolated "$1" run --standalone -m "$2" "Reply with exactly: ok" >"$tmp" 2>&1 &
  pid=$!
  probe_guard "$pid" "$PROBE_TIMEOUT" &
  gp=$!
  wait "$pid"; rc=$?
  kill "$gp" 2>/dev/null; wait "$gp" 2>/dev/null
  out="$(cat "$tmp" 2>/dev/null)"
  rm -f "$tmp"
  printf '%s' "$out"
  [ $rc -eq 0 ] || return 1
  printf '%s' "$out" | grep -Eq "$DEAD_PATTERNS" && return 1
  return 0
}

# Probe + record into the shared health store. $1=acct_dir $2=model
# rc: 0 alive · 1 transient/quota dead · 2 auth dead (never retry this session)
probe_pair() {
  local d="$1" m="$2" out rc t0 t1 ms
  t0=$(date +%s)
  out=$(check_pair "$d" "$m"); rc=$?
  t1=$(date +%s); ms=$(( (t1 - t0) * 1000 ))
  if [ $rc -eq 0 ]; then
    stats_record "$m" ok "$ms"
    return 0
  fi
  local why
  why=$(printf '%s' "$out" | grep -Eo "$DEAD_PATTERNS" | head -1)
  if printf '%s' "$out" | grep -Eq "$AUTH_PATTERNS"; then
    log "  auth failure on $m: ${why:-rc=$rc}"
    stats_record "$m" auth "$ms" "$why"
    return 2
  fi
  if printf '%s' "$out" | grep -Eq "$QUOTA_PATTERNS"; then
    # The wallet is empty or the quota is spent: the account's problem, not
    # the model's. Park it for minutes, not for a day.
    log "  quota exhausted on $m: ${why:-rc=$rc}"
    stats_record "$m" limit "$ms" "$why"
    return 1
  fi
  log "  dead: ${why:-rc=$rc}"
  stats_record "$m" fail "$ms" "$why"
  return 1
}

check_pair_retry() { # same as check_pair but retries transient 5xx (max 2 retries)
  local d="$1" m="$2" try out rc
  for try in 1 2 3; do
    out=$(check_pair "$d" "$m"); rc=$?
    if [ $rc -eq 0 ]; then printf '%s' "$out"; return 0; fi
    if printf '%s' "$out" | grep -Eq "$RETRYABLE_RE" && [ "$try" -lt 3 ]; then
      log "  transient error on $m (try $try/3), retrying"
      sleep 3
    else
      printf '%s' "$out"; return 1
    fi
  done
}

acct_env_ok() { # $1=acct_dir: has data/opencode/auth.json with credentials
  [ -s "$1/data/opencode/auth.json" ]
}


new_account_skeleton() { # $1=name -> creates isolated tree, prints dir
  local name="$1"
  local d="$CFG/$name"
  [ -n "$name" ] || die "account name required"
  case "$name" in *[/.]*|"."|"..") die "bad account name: $name" ;; esac
  mkdir -p "$d/home" "$d/data" "$d/config" "$d/state" "$d/cache"
  printf '%s\n' "$d"
}

register_account() { # $1=name $2=api_key  (opencode auth login needs a TTY, so write auth.json)
  local d
  d="$(new_account_skeleton "$1")"
  mkdir -p "$d/data/opencode"
  python3 - "$d/data/opencode/auth.json" "$2" <<'PY'
import json,sys
path,key=sys.argv[1],sys.argv[2]
try: d=json.load(open(path))
except Exception: d={}
d['opencode']={'type':'api','key':key}
json.dump(d,open(path,'w'),indent=2)
PY
  log "account '$1' registered at $d"
  exit 0
}

# subcommand: register an account
if [ -n "$ADD_NAME" ]; then
  [ -n "$ADD_KEY" ] || die "usage: $0 --add-account NAME API_KEY"
  register_account "$ADD_NAME" "$ADD_KEY"
fi
# no prompt (and not --list) => interactive mode: probe, then open the TUI
INTERACTIVE=0
[ -n "$PROMPT" ] || [ "$LIST" = 1 ] || INTERACTIVE=1

# ---- collect accounts (dirs with auth.json), sorted by mtime desc (recent wins) ----
# Sorted in python, not `find -exec stat -f '%m %N'`: in some environments the
# stat reached through -exec is GNU, where -f means "filesystem" — the pipeline
# then yields block sizes and "apfs" instead of paths, every account name
# becomes a number, and the skill reports its whole catalogue as DEAD while a
# perfectly valid auth.json sits on disk. python3 is already a dependency.
accounts=()
if [ -d "$CFG" ]; then
  while IFS= read -r name; do
    [ -n "$name" ] && accounts+=("$name")
  done < <(python3 - "$CFG" <<'PY'
import os, sys
root = sys.argv[1]
rows = []
for name in os.listdir(root):
    p = os.path.join(root, name, "data", "opencode", "auth.json")
    try:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            rows.append((os.path.getmtime(p), name))
    except OSError:
        pass
for _, name in sorted(rows, reverse=True):
    print(name)
PY
)
fi

[ "${#accounts[@]}" -gt 0 ] || die "no accounts in $CFG (add one: $0 --add-account NAME KEY)"

# ---- --list: probe matrix ----
if [ "$LIST" = 1 ]; then
  log "probing ${#accounts[@]} account(s) x models (each probe costs one free-tier ask)"
  for a in "${accounts[@]}"; do
    d="$CFG/$a"
    printf '%s\n' "account: $a  $(acct_env_ok "$d" && echo '[auth ok]' || echo '[NO AUTH]')"
    if [ "$PIN_MODEL" != "" ]; then ml="$PIN_MODEL"; else ml=$MODELS; fi
    for m in $(rotation_order $ml); do
      probe_pair "$d" "$m"
      case $? in
        0) printf '  %-45s OK\n' "$m" ;;
        2) printf '  %-45s AUTH DEAD (paused)\n' "$m" ;;
        *) printf '  %-45s DEAD\n' "$m" ;;
      esac
    done
  done
  exit 0
fi

# ---- pick account ----
STATE="$CFG/.state.json"
pick_account() {
  [ -n "$PIN_ACCOUNT" ] && { [ -d "$CFG/$PIN_ACCOUNT" ] || die "no such account: $PIN_ACCOUNT"; printf '%s' "$PIN_ACCOUNT"; return; }
  if [ "$KEEP" = 1 ] && [ -f "$STATE" ]; then
    local k; k=$(python3 -c "import json,sys;print(json.load(open('$STATE')).get('account',''))" 2>/dev/null)
    if [ -n "$k" ] && [ -d "$CFG/$k" ]; then printf '%s' "$k"; return; fi
  fi
  # newest registered account first (mtimes were sorted above)
  printf '%s' "${accounts[0]}"
}

ACCOUNT="$(pick_account)"
D="$CFG/$ACCOUNT"
acct_env_ok "$D" || die "account '$ACCOUNT' has no data/opencode/auth.json"

# ---- pick model (probe free models on the chosen account) ----
pick_model() {
  [ -n "$PIN_MODEL" ] && { printf '%s' "$PIN_MODEL"; return; }
  if [ "$KEEP" = 1 ] && [ -f "$STATE" ]; then
    local m; m=$(python3 -c "import json,sys;print(json.load(open('$STATE')).get('model',''))" 2>/dev/null)
    if [ -n "$m" ] && check_pair "$D" "$m" >/dev/null 2>&1; then printf '%s' "$m"; return; fi
  fi
  # Best-first: healthy by stability; after a failure its family leads the next
  # model tried (family-preserving failover), so the same model character is
  # kept instead of jumping to an unrelated one.
  local m rc next
  local -a remaining=($MODELS) rest
  FAMILY_OF=""
  while [ "${#remaining[@]}" -gt 0 ]; do
    # Re-ordered on every pass: after a failure the dead model's family leads.
    next="$(rotation_order "${remaining[@]}" | head -1)"
    [ -n "$next" ] || break
    rest=()
    for m in "${remaining[@]}"; do [ "$m" = "$next" ] || rest+=("$m"); done
    remaining=("${rest[@]}")

    log "probe $ACCOUNT / $next"
    probe_pair "$D" "$next"; rc=$?
    [ $rc -eq 0 ] && { printf '%s' "$next"; FAMILY_OF=""; return 0; }
    FAMILY_OF="$next"
    [ $rc -eq 2 ] && break   # auth error: whole account is out, stop probing
  done
  FAMILY_OF=""
  return 1
}

MODEL="$(pick_model)" || {
  # chosen account exhausted: rotate to next account with a working free model
  log "account $ACCOUNT exhausted for all free models — rotating"
  next=""
  for a in "${accounts[@]}"; do
    [ "$a" = "$ACCOUNT" ] && continue
    ad="$CFG/$a"; acct_env_ok "$ad" || continue
    # Same best-first loop as pick_model: health order, family failover, and a
    # hard stop on auth errors (that account is out, not just one model).
    remaining=($MODELS)
    FAMILY_OF=""
    while [ "${#remaining[@]}" -gt 0 ]; do
      nx="$(rotation_order "${remaining[@]}" | head -1)"
      [ -n "$nx" ] || break
      rest=()
      for m2 in "${remaining[@]}"; do [ "$m2" = "$nx" ] || rest+=("$m2"); done
      remaining=("${rest[@]}")
      log "probe $a / $nx"
      probe_pair "$ad" "$nx"; rc2=$?
      [ $rc2 -eq 0 ] && { next="$a"; nmodel="$nx"; break; }
      FAMILY_OF="$nx"
      [ $rc2 -eq 2 ] && break
    done
    FAMILY_OF=""
    [ -n "$next" ] && break
  done
  [ -n "$next" ] || die "all accounts exhausted for models: $MODELS"
  ACCOUNT="$next"; D="$CFG/$ACCOUNT"; MODEL="$nmodel"
}

log "chosen account=$ACCOUNT model=$MODEL"

# Machine-readable attribution. The Python supervisor cannot see inside this
# script, so without this line the journal's `model` column stays empty and a
# rotation has no real starting point. The marker is a dedicated one on
# purpose: the agent's own answer is printed to the same stream and may
# contain the words "model=" or "account=".
log "pobisk-attr: model=$MODEL account=$ACCOUNT"

# ---- remember working pair ----
python3 - "$STATE" "$ACCOUNT" "$MODEL" <<'PY'
import json,sys
json.dump({'account':sys.argv[2],'model':sys.argv[3],'ts':__import__('time').time()},open(sys.argv[1],'w'))
PY

# ---- launch ----
if [ "$INTERACTIVE" = 1 ]; then
  log "run: model=$MODEL account=$ACCOUNT (interactive TUI)"
  # run_isolated RETURNS when the TUI exits -> propagate its exit code and stop
  run_isolated "$D" -m "$MODEL"
  exit $?
fi
log "run: model=$MODEL account=$ACCOUNT stall=${STALL_WINDOW}s prompt=$(printf '%s' "$PROMPT" | head -c 80)"
OUT_FILE="$(mktemp "${TMPDIR:-/tmp}/oc-run.XXXXXX")" || OUT_FILE=/dev/null
run_isolated "$D" run --standalone -m "$MODEL" "$PROMPT" >"$OUT_FILE" 2>&1 &
RUN_PID=$!
_heartbeat "$RUN_PID" "$MODEL" "running"
stall_guard "$RUN_PID" "$D/data" "$OUT_FILE" &
GUARD_PID=$!
wait "$RUN_PID"
RC=$?
kill "$GUARD_PID" 2>/dev/null; wait "$GUARD_PID" 2>/dev/null
RUN_OUT="$(cat "$OUT_FILE" 2>/dev/null)"
cat "$OUT_FILE"
rm -f "$OUT_FILE"

# Record what the RUN actually did. Until now only the liveness probe
# reached the shared store, so the health of a model was judged by whether it
# could answer "ok" — and a model that passes every probe but fails the actual
# task stayed "healthy" forever. The run is the signal that matters.
if [ $RC -eq 143 ] || [ $RC -eq 137 ]; then
  log "run killed after ${STALL_WINDOW}s of silence — model is mute"
  echo "opencode-rotate: stalled ${STALL_WINDOW}s" >&2
  stats_record "$MODEL" fail "" "stalled ${STALL_WINDOW}s"
  exit $RC
fi
if [ $RC -eq 0 ] && printf '%s' "$RUN_OUT" | grep -Eq "$DEAD_PATTERNS"; then
  # rc 0 but the provider printed a dead pattern (quota spent, model
  # disabled): the run did not really succeed, and recording `ok` here
  # would teach the store that a broken model is a healthy one.
  DEAD_WHY="$(printf '%s' "$RUN_OUT" | grep -Eo "$DEAD_PATTERNS" | head -1)"
  log "  run reported a dead pattern: $DEAD_WHY"
  if printf '%s' "$RUN_OUT" | grep -Eq "$AUTH_PATTERNS"; then
    stats_record "$MODEL" auth "" "$DEAD_WHY"
  elif printf '%s' "$RUN_OUT" | grep -Eq "$QUOTA_PATTERNS"; then
    stats_record "$MODEL" limit "" "$DEAD_WHY"
  else
    stats_record "$MODEL" fail "" "$DEAD_WHY"
  fi
  exit $RC
fi
if [ $RC -eq 0 ]; then
  stats_record "$MODEL" ok ""
else
  stats_record "$MODEL" fail "" "run rc=$RC"
fi
exit $RC

