---
name: cline
license: MIT
description: >-
  Run the Cline CLI (https://github.com/cline/cline) as a supervised agent that
  never stalls on "Daily free model limit reached". Activates when the user says
  "cline", "запусти cline", "clineROTATE", "кончились лимиты cline", "free model
  limit", "Daily free model limit reached", "переключи аккаунт cline", or when a
  cline run dies with INFERENCE_CAP_ERROR / 429 on a free model. Rotates through
  the free models (DeepSeek V4.1 Flash, Muse Spark 1.3 Contributor, GLM-5.3-Flash,
  Solar Pro 4, Laguna S 2.1) and, when all of them are exhausted on one account,
  fails over to the next account in ~/.cline/accounts. Do NOT apply for paid
  ClinePass/credits runs or for non-cline agents.
---

# cline — supervised Cline CLI runner

`cline` (https://github.com/cline/cline) dies on free-tier accounts with:

```
Daily free model limit reached
You've reached today's free usage limit for this model.
Try again in 23h 45m"}} or select another model.
```

The CLI has **no built-in rotation**, so an unattended run stops there. This
skill wraps `cline` in a supervisor that picks a working model itself and, when
every free model on an account is exhausted, switches to another account.

## The one non-obvious fact (read this first)

In **non-interactive** mode `cline` exits `1` **and prints the limit banner** —
the exit code does not distinguish "limit reached" from a real error. So the
supervisor matches on the message text, not on `$?`. Never "fix" this by
checking the exit code alone: you will rotate on genuine failures and burn the
whole rotation for nothing.

The free-model catalogue lives inside the `cline` binary in two tables: the
`kilo` router catalogue (free tiers end in `:free`, plus the `openrouter/free`
router) and the core `cline` namespace (`cline-free/*` and a few ids such as
`z-ai/glm-5.3-flash` that carry no marker at all). `cline_Models_Free_Make.sh` walks both
and writes `cline-models-free.txt`. The supervisor pins the model per-run with
`-m`, so a stale model saved in an account's `providers.json` can never be
trusted blindly.

## Layout

```
<repo>/.agents/skills/cline/
├── SKILL.md              ← this file
├── cline-rotate.sh       ← the supervisor (bash, no deps beyond cline + python3)
├── cline_Models_Free_Make.sh         ← regenerates the catalogue below from the cline binary
└── cline-models-free.txt ← every free model id, one per line, no names (generated)
```

`cline-rotate.sh` shares `~/.ai-rotate/model-stats.json` with the OpenCode and
FreeBuff supervisors via `../_shared/model-stats.py`: probe results are reused
across skills, and rotation order follows measured stability instead of the file
order. See [`../_shared/README.md`](../_shared/README.md). Disable with
`CLINE_NO_STATS=1`.

`models_4_rotattion.txt` is your own priority list of display names; it is
independent of the catalogue and the supervisor does **not** read it — it exists
for you to eyeball which models matter most.

## Keep the catalogue fresh

The `/models` picker draws its list from the `cline` binary, and that list
changes with every update. Regenerate after an upgrade:

```bash
"$SKILL_DIR/cline_Models_Free_Make.sh"     # rewrites cline-models-free.txt
```

The supervisor reads that file at startup, so a stale catalogue silently means a
shorter rotation. If the file is missing the supervisor falls back to a
built-in list and logs a warning — it never blocks on it.

## Resolve the runner for this session

The skill lives in the repo, so resolve it by walking up from `$PWD` — this
works from any subdirectory and does not hard-code an absolute path:

```bash
resolve_runner() {
  local d="$PWD"
  while [[ "$d" != "/" ]]; do
    [[ -x "$d/.agents/skills/cline/cline-rotate.sh" ]] && { echo "$d/.agents/skills/cline/cline-rotate.sh"; return 0; }
    d="$(dirname "$d")"
  done
  return 1
}
RUNNER="$(resolve_runner)" || { echo "cline skill not found under $PWD"; exit 1; }
```

## How to run a task

The supervisor mirrors the `cline` prompt/flags you already know:

```bash
# act mode, auto-approve — replaces your manual one-liners
"$RUNNER" "сделай рассылку материалов про игру"

# no prompt: probe first, then hand the terminal to cline as an interactive REPL
"$RUNNER"

# plan mode
"$RUNNER" -p "спланируй фичу"

# resume a session
"$RUNNER" --session 1789848344724_b9kw5 "продолжи"

# pin the rotation / extra cline flags
CLINE_MODELS="cline-free/solar-pro4 z-ai/glm-5.3-flash" \
CLINE_EXTRA_ARGS="--timeout 600" \
"$RUNNER" "..."
```

Prefer the supervisor over bare `cline` whenever the run is unattended (cron,
`--zen`, a script, a long task you walk away from), and also for interactive
sessions: with no prompt the supervisor still probes first, so the REPL opens on
a model that is not already over its daily cap. For a pair-programming session
where the human is watching and ready to `/model` by hand, plain `cline` is fine.

## What the supervisor decides for you

| Situation                                | Action                                        |
| ---------------------------------------- | --------------------------------------------- |
| Model prints the daily-limit banner      | next model in the rotation                    |
| All models exhausted on one account      | next account in `~/.cline/accounts`           |
| Account-level failure (auth/other error) | skip the whole account, don't burn models     |
| All accounts exhausted                   | exit 5, log `ABORT: all accounts exhausted`   |
| Loop guard tripped                       | exit 4, never spins forever                   |

Exit codes: `0` task ok · `1` task failed (propagated from cline) · `3` no
accounts found · `4` loop guard · `5` all accounts exhausted. An interactive
launch has no meaningful exit code of its own — the supervisor `exec`s cline, so
the REPL replaces the supervisor process and hands its exit code back.

## Accounts

Accounts are directories under `~/.cline/accounts/<name>/` containing
`settings/providers.json`. An account joins the rotation **only if the model
recorded in its `providers.json` is one of the free models in the rotation** —
an account pinned to a paid or stale model (`openrouter/free`, a revoked ClinePass
model…) is skipped, because that model cannot be probed for free.

Add an account:

```bash
mkdir -p ~/.cline/accounts/<new-account>
cline --data-dir ~/.cline/accounts/<new-account> auth cline   # log in once
cline --data-dir ~/.cline/accounts/<new-account> -m cline-free/deepseek-v4.1-flash "ok"
```

The account is picked up automatically on the next run — no config edit.

## Inspecting state

```bash
# what the supervisor did
tail -n 40 ~/.cline/logs/cline-rotate.log

# which model each account is pinned to + token expiry
python3 - <<'PY'
import json, glob, os, datetime
for p in sorted(glob.glob(os.path.expanduser("~/.cline/accounts/*/settings/providers.json"))):
    d = json.load(open(p))
    s = d["providers"]["cline"]["settings"]
    exp = s["auth"].get("expiresAt")
    print(s["auth"]["metadata"]["userInfo"].get("email"), "->", s.get("model"),
          "expires", datetime.datetime.fromtimestamp(exp / 1000, datetime.UTC).isoformat() if exp else "?")
PY
```

## Loop-safety invariants (do not break these)

1. **Probing must never mutate state.** Probes run with `--auto-approve false
   --retries 1 --timeout 45` and a throwaway prompt; only the winning model is
   written into `providers.json`.
2. **Two independent guards against infinite rotation**: a per-invocation attempt
   counter (`accounts × models + 5`) and `CLINE_MAX_ACCOUNT_ROUNDS` (default: one
   full sweep of the account list). Reaching either is a hard exit, never a retry.
3. **Detection by text, not exit code** — see the note above.
4. **Account-level failure ≠ model-level failure.** A non-limit error skips the
   account entirely; only a confirmed limit banner consumes the next model.

## Testing the supervisor without burning real quota

Stub `cline` and point the supervisor at fake account dirs — this is how every
branch (model rotation, account fail-over, dead-account skip, all-exhausted) was
verified:

```bash
mkdir -p /tmp/shim /tmp/accs/{a,b}/settings
cat > /tmp/shim/cline <<'EOF'
#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do case "$1" in --data-dir|-m|--retries|--timeout) shift 2;; *) shift;; esac; done
printf 'error: Daily free model limit reached\nYou have reached today'\''s free usage limit for this model.\n'
EOF
chmod +x /tmp/shim/cline
cp ~/.cline/accounts/airegist01/settings/providers.json /tmp/accs/a/settings/
cp ~/.cline/accounts/ivrabo/settings/providers.json     /tmp/accs/b/settings/
CLINE_BIN=/tmp/shim/cline CLINE_ACCOUNTS_DIR=/tmp/accs \
  "$RUNNER" "test"                                        # expect: sweep then exit 5
```

## Troubleshooting

- **`all accounts exhausted`** — every free model is capped until ~UTC midnight.
  Add an account, or `CLINE_MODELS="cline-pass/..."` if you have ClinePass.
- **Account silently skipped** — its `providers.json` model is outside the
  rotation; the log line says which model. Re-pin with `cline -m cline-free/...`.
- **Probe timeouts** — raise `CLINE_PROBE_TIMEOUT` (default 45s) for slow links.
- **Token expired** — the account fails at probe time and is skipped; re-run
  `cline auth` for that `--data-dir`.
