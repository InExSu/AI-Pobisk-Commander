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
# Env: OC_MODELS, OC_ACCOUNTS_DIR, OC_BIN, OC_PROBE_TIMEOUT
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

FREE_MODELS_DEFAULT="opencode/mimo-v2.5-free opencode/ling-3.0-flash-fin-free \
opencode/nemotron-3-ultra-free opencode/nemotron-3.5-lightning-free \
opencode/muse-spark-1.3-contributor-free opencode/muse-spark-1.2-contributor-free \
opencode/jev-1.13-free"
MODELS="${OC_MODELS:-$FREE_MODELS_DEFAULT}"
PROBE_TIMEOUT="${OC_PROBE_TIMEOUT:-25}"

# Fixed error texts of a dead pair (from the binary + live probes, see SKILL.md)
DEAD_PATTERNS='FreeUsageLimitError|GoUsageLimitError|Free limit reached|Usage limit reached|Rate limit exceeded|[Rr]ate limit|Quota exceeded|No payment method|payment method here|Model is disabled|Model not found|Invalid API key|not authenticated|Unauthorized|FreeTierError|CreditsError'
RETRYABLE_RE='Internal server error|statusCode.*5[0-9][0-9]|isRetryable'
# Credential failures: retrying never fixes them, so the circuit breaker parks
# the model instead of counting it as a transient failure.
AUTH_PATTERNS='Invalid API key|not authenticated|Unauthorized|No payment method|payment method here'

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

check_pair() { # $1=acct_dir $2=model_id -> prints answer, rc=0 if alive
  local out
  out=$(run_isolated "$1" run -m "$2" "Reply with exactly: ok" 2>&1)
  local rc=$?
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
accounts=()
if [ -d "$CFG" ]; then
  while IFS= read -r f; do
    rel="${f#"$CFG"/}"          # <name>/data/opencode/auth.json
    accounts+=("${rel%%/*}")
  done < <(find "$CFG" -mindepth 4 -maxdepth 4 -path '*/data/opencode/auth.json' -type f \
           -exec stat -f '%m %N' {} \; 2>/dev/null | sort -rn | awk '{print $NF}')
fi
# de-dup, keep order
if [ "${#accounts[@]}" -gt 0 ]; then
  mapfile -t accounts < <(printf '%s\n' "${accounts[@]}" | awk '!seen[$0]++')
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
log "run: model=$MODEL account=$ACCOUNT prompt=$(printf '%s' "$PROMPT" | head -c 80)"
run_isolated "$D" run -m "$MODEL" "$PROMPT"

