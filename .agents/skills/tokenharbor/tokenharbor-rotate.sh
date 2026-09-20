#!/usr/bin/env bash
# tokenharbor-rotate — CLI for Token Harbor's free models.
#
# Token Harbor (https://tokenharbor.ai) serves an OpenAI-compatible endpoint and
# has no CLI client, so this script is the client: `ask`, `models`, `probe`,
# `parallel`, `rpm`.
#
# Free models are the ones whose id ends in `:free` AND whose pricing block is
# zero on both input and output. `th-orchestra` also shows zero pricing in the
# catalogue but returns 402 on a real call — it is NOT free; it is excluded.
#
# Usage:
#   ./tokenharbor-rotate.sh ask "prompt"
#   ./tokenharbor-rotate.sh ask -m MODEL "prompt"
#   ./tokenharbor-rotate.sh models
#   ./tokenharbor-rotate.sh probe
#   ./tokenharbor-rotate.sh parallel "p1" "p2" ...
#
# Env:
#   TOKENHARBOR_API_KEY  key (else read from the secret store)
#   TH_MODELS            space-separated model ids
#   TH_MAX_TOKENS        default 2048
#   TH_TIMEOUT           seconds per request, default 180
#
# Exit codes: 0 ok · 1 usage · 2 no API key · 3 all models dead

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
SHARED_DIR="$(cd "$SKILL_DIR/.." >/dev/null 2>&1 && pwd)/_shared"

# shellcheck source=../_shared/require-secret.sh
. "$SHARED_DIR/require-secret.sh"
require_secrets TOKENHARBOR_API_KEY:tokenharbor

API="${TH_API_BASE:-https://tokenharbor.ai/v1}"
MAX_TOKENS="${TH_MAX_TOKENS:-2048}"
TIMEOUT="${TH_TIMEOUT:-180}"

# The four :free ids that answered a real completion (2026-09-20). Refreshed by
# `models --refresh`, which re-reads the live catalogue and filters on price.
DEFAULT_MODELS=(
  "qwen3.8-flash:free"
  "deepseek-v4.1-flash:free"
  "deepseek-v4-flash:free"
  "mimo-v2.5:free"
)

if [[ -n "${TH_MODELS:-}" ]]; then
  MODELS=(${TH_MODELS})
else
  MODELS=("${DEFAULT_MODELS[@]}")
fi

ask_model() { # $1=model $2=prompt
  python3 - "$1" "$2" "$API" "$MAX_TOKENS" "$TIMEOUT" <<'PY'
import json, os, sys, urllib.error, urllib.request

model, prompt, api, max_tokens, timeout = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]))
body = json.dumps({"model": model, "max_tokens": int(max_tokens),
                   "messages": [{"role": "user", "content": prompt}]}).encode()
req = urllib.request.Request(
    api + "/chat/completions", data=body,
    headers={"Authorization": "Bearer " + os.environ["TOKENHARBOR_API_KEY"],
             "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
except urllib.error.HTTPError as e:
    sys.stderr.write("HTTP %s: %s\n" % (e.code, e.read()[:200].decode("utf8", "replace")))
    sys.exit(1)
except Exception as e:
    sys.stderr.write("%s\n" % type(e).__name__)
    sys.exit(1)
msg = (d.get("choices") or [{}])[0].get("message") or {}
sys.stdout.write(msg.get("content") or msg.get("reasoning_content") or "")
PY
}

CMD="${1:-ask}"
[[ $# -gt 0 ]] && shift

case "$CMD" in
models)
  REFRESH=0
  [[ "${1:-}" == "--refresh" ]] && REFRESH=1
  if [[ $REFRESH -eq 1 ]]; then
    # Live catalogue, filtered on real zero pricing. th-orchestra is dropped:
    # it advertises 0/0 but answers 402, so the price test alone is not enough.
    python3 - "$API" <<'PY'
import json, os, sys, urllib.request
api = sys.argv[1]
req = urllib.request.Request(
    api + "/models",
    headers={"Authorization": "Bearer " + os.environ["TOKENHARBOR_API_KEY"]})
with urllib.request.urlopen(req, timeout=30) as r:
    d = json.load(r)
for m in d.get("data") or []:
    p = m.get("pricing") or {}
    if p.get("input_usd_per_1m") == 0 and p.get("output_usd_per_1m") == 0 \
       and m["id"].endswith(":free"):
        print(m["id"])
PY
  else
    printf '%s\n' "${MODELS[@]}"
  fi
  ;;

probe)
  python3 - "$API" "$TIMEOUT" "${MODELS[@]}" <<'PY'
import concurrent.futures, json, os, sys, time, urllib.error, urllib.request
api, timeout = sys.argv[1], float(sys.argv[2])
models = sys.argv[3:]
def probe(mid):
    body = json.dumps({"model": mid, "max_tokens": 8,
                       "messages": [{"role": "user", "content": "say ok"}]}).encode()
    req = urllib.request.Request(
        api + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + os.environ["TOKENHARBOR_API_KEY"],
                 "Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.load(r)
        return mid, 200, time.time() - t
    except urllib.error.HTTPError as e:
        return mid, e.code, time.time() - t
    except Exception as e:
        return mid, type(e).__name__, time.time() - t
with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as ex:
    res = list(ex.map(probe, models))
ok = 0
for mid, code, dt in res:
    if code == 200:
        ok += 1
        print("  OK    %-30s %.1fs" % (mid, dt))
    else:
        print("  DEAD  %-30s %s" % (mid, code))
print("\n%d/%d alive" % (ok, len(res)))
PY
  ;;

ask)
  PIN=""
  if [[ "${1:-}" == "-m" || "${1:-}" == "--model" ]]; then PIN="${2:-}"; shift 2; fi
  PROMPT="${*:-}"
  [[ -z "$PROMPT" ]] && { echo "tokenharbor-rotate ask: empty prompt" >&2; exit 1; }
  if [[ -n "$PIN" ]]; then
    ask_model "$PIN" "$PROMPT" && exit 0
    echo "tokenharbor-rotate: pinned model $PIN failed" >&2
    exit 3
  fi
  ORDER="$(printf '%s\n' "${MODELS[@]}" |
    python3 "$SHARED_DIR/model-stats.py" order tokenharbor --models "${MODELS[@]}" 2>/dev/null ||
    printf '%s\n' "${MODELS[@]}")"
  for m in $ORDER; do
    if OUT="$(ask_model "$m" "$PROMPT" 2>/tmp/th_err)"; then
      python3 "$SHARED_DIR/model-stats.py" record tokenharbor "$m" ok "" >/dev/null 2>&1
      printf '%s\n' "$OUT"
      exit 0
    fi
    echo "  dead: $m ($(head -c 60 /tmp/th_err))" >&2
    python3 "$SHARED_DIR/model-stats.py" record tokenharbor "$m" fail "" "$(head -c 80 /tmp/th_err)" >/dev/null 2>&1
  done
  rm -f /tmp/th_err
  echo "tokenharbor-rotate: all models dead" >&2
  exit 3
  ;;

parallel)
  PAR_PROMPTS=("$@")
  [[ ${#PAR_PROMPTS[@]} -eq 0 ]] && { echo "tokenharbor-rotate parallel: pass prompts as arguments" >&2; exit 1; }
  export TH_POOL="${MODELS[*]}"
  python3 - "$API" "$MAX_TOKENS" "$TIMEOUT" "${PAR_PROMPTS[@]}" <<'PY'
import concurrent.futures, json, os, sys, urllib.error, urllib.request
api, max_tokens, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
models = os.environ["TH_POOL"].split()
prompts = [p for p in sys.argv[4:] if p]
def call(a):
    i, mid, p = a
    body = json.dumps({"model": mid, "max_tokens": int(max_tokens),
                       "messages": [{"role": "user", "content": p}]}).encode()
    req = urllib.request.Request(
        api + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + os.environ["TOKENHARBOR_API_KEY"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        m = (d.get("choices") or [{}])[0].get("message") or {}
        return i, mid, 0, m.get("content") or m.get("reasoning_content") or ""
    except urllib.error.HTTPError as e:
        return i, mid, e.code, e.read()[:150].decode("utf8", "replace")
    except Exception as e:
        return i, mid, 0, type(e).__name__
jobs = [(i, models[i % len(models)], p) for i, p in enumerate(prompts)]
with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(jobs), 8)) as ex:
    res = list(ex.map(call, jobs))
for i, mid, code, text in sorted(res):
    print("=== [%d] %s%s" % (i, mid, "" if code == 0 else "  (HTTP %s)" % code))
    print(text)
PY
  ;;

-h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
*) echo "tokenharbor-rotate: unknown subcommand '$CMD'" >&2; exit 1 ;;
esac
