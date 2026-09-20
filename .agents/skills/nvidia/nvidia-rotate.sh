#!/usr/bin/env bash
# nvidia-rotate — supervisor + CLI for NVIDIA NIM free models.
#
# NVIDIA NIM exposes an OpenAI-compatible endpoint and NO official CLI client,
# so this script IS the CLI: `ask`, `models`, `probe`, `bench`, `parallel`.
#
# The rate limit that shapes everything: NIM allows 40 requests/minute PER
# MODEL, not per key. So 9 models x 40 = up to 360 RPM in aggregate, as long as
# requests are spread across distinct model ids. Serial rotation across models
# therefore wastes capacity — `--parallel` is the mode that actually uses it.
#
# Usage:
#   ./nvidia-rotate.sh ask "prompt"                 # one model, auto-picked
#   ./nvidia-rotate.sh ask -m MODEL "prompt"        # pin a model
#   ./nvidia-rotate.sh models                       # list catalogued models
#   ./nvidia-rotate.sh probe                        # test which models answer
#   ./nvidia-rotate.sh parallel "p1" "p2" ...       # spread prompts across models
#   ./nvidia-rotate.sh rpm                          # show the per-model budget
#
# Env:
#   NVIDIA_API_KEY        key (else read from the secret store)
#   NVIDIA_MODELS         space-separated model ids
#   NVIDIA_RPM_PER_MODEL  per-model budget        (default: 40)
#   NVIDIA_MAX_TOKENS     max_tokens per answer   (default: 2048)
#   NVIDIA_TIMEOUT        seconds per request     (default: 240)
#   AI_ROTATE_DIR         secret + health store   (default: ~/.ai-rotate)
#
# Exit codes: 0 ok · 1 usage · 2 no API key · 3 all models dead

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
SHARED_DIR="$(cd "$SKILL_DIR/.." >/dev/null 2>&1 && pwd)/_shared"

# shellcheck source=../_shared/require-secret.sh
. "$SHARED_DIR/require-secret.sh"
require_secrets NVIDIA_API_KEY:nvidia

API="${NVIDIA_API_BASE:-https://integrate.api.nvidia.com/v1}"
RPM_PER_MODEL="${NVIDIA_RPM_PER_MODEL:-40}"
MAX_TOKENS="${NVIDIA_MAX_TOKENS:-2048}"
TIMEOUT="${NVIDIA_TIMEOUT:-240}"
MODELS_FILE="$SKILL_DIR/nvidia-models-free.txt"

# Only these answered a real chat completion during the 2026-09-20 audit of the
# full 82-model catalogue. The catalogue advertises far more; most return 404
# ("Function ... not found") because they are not deployed on the free tier.
DEFAULT_MODELS=(
  "z-ai/glm-5.3-flash"
  "moonshotai/kimi-k3"
  "nvidia/nemotron-3-super-120b-a12b"
  "nvidia/nemotron-3-ultra-550b-a55b"
  "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
  "nvidia/nemotron-3.5-lightning-30b-a3b"
  "openai/gpt-oss-20b"
  "poolside/laguna-xs-2.1"
  "meta/muse-glimmer-30b"
  "mistralai/mistral-nemotron"
  "meta/llama-3.2-11b-vision-instruct"
)

read_models() {
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    printf '%s\n' "${line%%$'\t'*}"
  done <"$MODELS_FILE"
}

if [[ -n "${NVIDIA_MODELS:-}" ]]; then
  MODELS=(${NVIDIA_MODELS})
elif [[ -f "$MODELS_FILE" ]]; then
  MODELS=($(read_models))
  [[ ${#MODELS[@]} -eq 0 ]] && MODELS=("${DEFAULT_MODELS[@]}")
else
  MODELS=("${DEFAULT_MODELS[@]}")
fi

# ── One chat completion. Prints the answer; rc 0 on HTTP 200. ─────────────────
# The prompt travels as an argv item, NOT on stdin: this python is fed via a
# heredoc, so stdin is already at EOF by the time the program runs (reading it
# would silently yield an empty prompt and the model answers "your message came
# through empty").
ask_model() { # $1=model $2=prompt
  python3 - "$1" "$2" "$API" "$MAX_TOKENS" "$TIMEOUT" <<'PY'
import json, os, sys, urllib.error, urllib.request

model, prompt, api, max_tokens, timeout = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]))
body = json.dumps({
    "model": model,
    "max_tokens": int(max_tokens),
    "messages": [{"role": "user", "content": prompt}],
}).encode()
req = urllib.request.Request(
    api + "/chat/completions", data=body,
    headers={"Authorization": "Bearer " + os.environ["NVIDIA_API_KEY"],
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
# Reasoning models (glm-5.3-flash, kimi-k3) put the answer in
# reasoning_content and leave content null when the token budget is tight.
text = msg.get("content") or msg.get("reasoning_content") or ""
sys.stdout.write(text)
PY
}

# ── Subcommands ───────────────────────────────────────────────────────────────
CMD="${1:-ask}"
[[ $# -gt 0 ]] && shift

case "$CMD" in
models)
  printf '%s\n' "${MODELS[@]}"
  ;;

rpm)
  printf 'per-model budget : %s RPM (NIM limits each model id separately)\n' "$RPM_PER_MODEL"
  printf 'models in pool   : %d\n' "${#MODELS[@]}"
  printf 'aggregate        : %d RPM\n' "$((RPM_PER_MODEL * ${#MODELS[@]}))"
  printf '\nserial rotation uses ~1 model -> %s RPM\n' "$RPM_PER_MODEL"
  printf 'parallel spread  uses all    -> %d RPM\n' "$((RPM_PER_MODEL * ${#MODELS[@]}))"
  ;;

probe)
  # Which models actually answer? One minimal completion each, in parallel
  # (per-model limits make parallel probing safe and much faster).
  python3 - "$API" "$TIMEOUT" "${MODELS[@]}" <<'PY'
import concurrent.futures, json, os, sys, time, urllib.error, urllib.request

api, timeout = sys.argv[1], float(sys.argv[2])
models = sys.argv[3:]

def probe(mid):
    body = json.dumps({"model": mid, "max_tokens": 8,
                       "messages": [{"role": "user", "content": "say ok"}]}).encode()
    req = urllib.request.Request(
        api + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + os.environ["NVIDIA_API_KEY"],
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

with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(models))) as ex:
    res = list(ex.map(probe, models))
ok = 0
for mid, code, dt in res:
    if code == 200:
        ok += 1
        print("  OK    %-52s %.1fs" % (mid, dt))
    else:
        print("  DEAD  %-52s %s" % (mid, code))
print("\n%d/%d alive" % (ok, len(res)))
PY
  ;;

ask)
  PIN=""
  if [[ "${1:-}" == "-m" || "${1:-}" == "--model" ]]; then
    PIN="${2:-}"; shift 2
  fi
  PROMPT="${*:-}"
  # Read stdin ONLY when it is a pipe/file. Run interactively with no prompt,
  # `cat` waits on the terminal forever — the script looks hung and prints
  # nothing (the exact bug: `nvidia-rotate.sh` with no args never returns).
  if [[ -z "$PROMPT" && ! -t 0 ]]; then
    PROMPT="$(cat)"
  fi
  if [[ -z "$PROMPT" ]]; then
    echo "usage: nvidia-rotate.sh ask [\"prompt\"]   (or pipe a prompt on stdin)" >&2
    echo "       echo 'prompt' | nvidia-rotate.sh ask" >&2
    exit 1
  fi

  if [[ -n "$PIN" ]]; then
    ask_model "$PIN" "$PROMPT" && exit 0
    echo "nvidia-rotate: pinned model $PIN failed" >&2
    exit 3
  fi

  # Try models in health order until one answers.
  ORDER="$(printf '%s\n' "${MODELS[@]}" |
    python3 "$SHARED_DIR/model-stats.py" order nvidia --models "${MODELS[@]}" 2>/dev/null ||
    printf '%s\n' "${MODELS[@]}")"
  for m in $ORDER; do
    if OUT="$(ask_model "$m" "$PROMPT" 2>/tmp/nv_err)"; then
      python3 "$SHARED_DIR/model-stats.py" record nvidia "$m" ok "" >/dev/null 2>&1
      printf '%s\n' "$OUT"
      exit 0
    fi
    echo "  dead: $m ($(head -c 60 /tmp/nv_err))" >&2
    python3 "$SHARED_DIR/model-stats.py" record nvidia "$m" fail "" "$(head -c 80 /tmp/nv_err)" >/dev/null 2>&1
  done
  rm -f /tmp/nv_err
  echo "nvidia-rotate: all models dead" >&2
  exit 3
  ;;

parallel)
  # Spread prompts across distinct model ids: each model keeps its own 40 RPM
  # budget, so N prompts on N models cost 1 request per model, not N on one.
  # Prompts come in as argv (stdin is the heredoc below, so it is at EOF) or,
  # when none are given, from a file named by $NVIDIA_PROMPTS_FILE.
  PAR_PROMPTS=("$@")
  if [[ ${#PAR_PROMPTS[@]} -eq 0 && -n "${NVIDIA_PROMPTS_FILE:-}" && -f "${NVIDIA_PROMPTS_FILE:-}" ]]; then
    mapfile -t PAR_PROMPTS <"$NVIDIA_PROMPTS_FILE"
  fi
  export NVIDIA_POOL="${MODELS[*]}"
  python3 - "$API" "$MAX_TOKENS" "$TIMEOUT" "${PAR_PROMPTS[@]}" <<'PY'
import concurrent.futures, json, os, sys, urllib.error, urllib.request

api, max_tokens, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
models = os.environ["NVIDIA_POOL"].split()
prompts = [p for p in sys.argv[4:] if p]
if not prompts:
    sys.exit("nvidia-rotate parallel: no prompts (pass them as arguments)")

def call(args):
    idx, mid, prompt = args
    body = json.dumps({"model": mid, "max_tokens": int(max_tokens),
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        api + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + os.environ["NVIDIA_API_KEY"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        return idx, mid, 0, msg.get("content") or msg.get("reasoning_content") or ""
    except urllib.error.HTTPError as e:
        return idx, mid, e.code, e.read()[:150].decode("utf8", "replace")
    except Exception as e:
        return idx, mid, 0, type(e).__name__

# Round-robin: consecutive prompts land on different model ids, which is what
# keeps each model under its own per-model cap.
jobs = [(i, models[i % len(models)], p) for i, p in enumerate(prompts)]
with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(jobs), 16)) as ex:
    res = list(ex.map(call, jobs))
for idx, mid, code, text in sorted(res):
    print("=== [%d] %s%s" % (idx, mid, "" if code == 0 else "  (HTTP %s)" % code))
    print(text)
PY
  ;;

-h|--help)
  sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  ;;
*)
  echo "nvidia-rotate: unknown subcommand '$CMD' (ask|models|probe|parallel|rpm)" >&2
  exit 1
  ;;
esac
