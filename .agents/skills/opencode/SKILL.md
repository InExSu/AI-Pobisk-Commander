---
name: opencode-rotate
description: Rotate OpenCode accounts & free models for `opencode run`. Use when OpenCode hits "Free limit reached", "No payment method", or you need a fresh free-tier account/model for a one-shot prompt.
---

# OpenCode Rotate: switch account/model for free-tier `opencode run`

## One non-obvious fact

OpenCode fully isolates state via `OPENCODE_TEST_HOME` (replaces `os.homedir()`,
`Global.ts` in the binary) + `XDG_DATA_HOME`/`XDG_CONFIG_HOME`/`XDG_STATE_HOME`/`XDG_CACHE_HOME`.
Per-account dirs live in `~/.opencode-accounts/<name>/{home,data,config,state,cache}`;
state inside is `{data,config,state,cache}/opencode/*` (auth.json in `data/`, model cache in `config/`).

Do not share `XDG_CONFIG_HOME` between accounts: `opencode run` installs npm
packages into `$XDG_CONFIG_HOME/opencode/node_modules` on every launch →
races and "package.json corrupted" errors. Always give each account its own config dir.
`--share` must be off (default) so runs never touch opencode.ai/sessions.

`opencode run -m <provider/model> "<prompt>"` is the only non-interactive mode;
exit 0 + answer on stdout means success.

## Free-tier reality (probed 2026-09-20, v1.18.31)

- Free models (id `opencode/<id>`, price 0 in `opencode models opencode`):
  `mimo-v2.5-free`, `ling-3.0-flash-fin-free`, `nemotron-3-ultra-free`,
  `nemotron-3.5-lightning-free`, `muse-spark-1.2-contributor-free`,
  `muse-spark-1.3-contributor-free`, `jev-1.13-free`.
- `big-pickle` is "Free" in docs but **disabled** (`Model is disabled`).
- `-nano` models (gpt-5-nano etc.) are NOT free without a payment method:
  `No payment method. Add a payment method here: https://opencode.ai/workspace/.../billing`.
- Free tier server-gated: plain curl to `https://opencode.ai/zen/v1/chat/completions`
  with the sk- key → 403 `FreeTierError` ("can only be used from within OpenCode").
  So pre-flight probing is only possible by running the CLI (probe = billable ask).
- When a free limit is hit, run fails with a message containing
  `FreeUsageLimitError` / `Usage limit reached` / `rate limit` / `Free limit reached`.
- `OPENCODE_DISABLE_AUTOUPDATE=1` in every invocation: opencode self-update
  would break paths mid-flight.

## Setup

```bash
# one-time per account (key from https://opencode.ai/auth → API Keys)
.agents/skills/opencode/opencode-rotate.sh --add-account oc-main sk-UYJF...
.agents/skills/opencode/opencode-rotate.sh --add-account oc-second sk-XXXX...
```

## Usage

```bash
OC=.agents/skills/opencode/opencode-rotate.sh

# probe matrix (accounts × free models; each probe = 1 free-tier ask)
$OC --list

# one-shot run with auto account/model rotation (probes only on miss)
$OC -- "fix the failing test in src/auth.ts"

# no prompt: probe, then open the interactive TUI on a live pair
$OC

# pin a model / account / reuse last working pair without probing
$OC --model opencode/mimo-v2.5-free -- "explain this diff"
$OC --account oc-second -- "..."
$OC --keep -- "continue the refactor"

# rotate accounts only, keep default model order
OC_MODELS="opencode/mimo-v2.5-free opencode/nemotron-3-ultra-free" $OC -- "..."
```

Exit codes: 0 = model answered; 1 = usage/config error; probe failures
rotate to next account/model automatically; total exhaustion dies with
"all accounts exhausted". With no prompt the script opens the interactive
TUI (`opencode -m <model>` under the account's isolated env) after probing,
so the session always starts on a working pair; the TUI's exit code is
propagated.

## Layout

- `.agents/skills/opencode/opencode_Models_Free_Make.sh` — regenerates `opencode-models-free.txt`
  from `opencode models --verbose opencode` (ids only, no names). Re-run after an opencode update.
- `.agents/skills/opencode/opencode-models-free.txt` — every free model id, one per line (generated).
- `.agents/skills/opencode/opencode-rotate.sh` — the only script; no deps beyond bash/python3.
- `~/.ai-rotate/model-stats.json` (via `../_shared/model-stats.py`) — shared health store:
  rotation order follows measured stability, cooldown after 3 failures, and a dead
  `deepseek/*` fails over to another `deepseek/*` first. See [`../_shared/README.md`](../_shared/README.md).
  Disable with `OC_NO_STATS=1`.
- `~/.opencode-accounts/<name>/{home,data,config,state,cache}/` — isolated env per account;
  credentials at `data/opencode/auth.json` (`{"opencode":{"type":"api","key":...}}`).
- `~/.opencode-accounts/.state.json` — last working (account, model) pair for `--keep`.

## Invariants

- Every opencode invocation goes through `run_isolated` (env -i + OPENCODE_TEST_HOME
  + all four XDG_* + OPENCODE_DISABLE_AUTOUPDATE=1). Never call `opencode` bare.
- A pair (account, model) is "alive" only if exit 0 AND output matches none of
  `FreeUsageLimitError|Usage limit reached|Free limit reached|Rate limit exceeded|
  Quota exceeded|No payment method|Model is disabled|Model not found|Invalid API key|
  FreeTierError|CreditsError|GoUsageLimitError`.
- First working model per account wins; on total failure the next account is probed.

## Testing

```bash
# syntax
bash -n .agents/skills/opencode/opencode-rotate.sh
# isolated probe without touching real state
OC_ACCOUNTS_DIR=$(mktemp -d) .agents/skills/opencode/opencode-rotate.sh --list
# real smoke (costs 1 free ask)
.agents/skills/opencode/opencode-rotate.sh -- "say ok"
```

## Differences vs .agents/skills/cline

- cline rotates inside a live TUI session (supervisor restarts the binary with a
  different model). OpenCode free tier is server-gated per HTTP call and
  `opencode run` is one-shot: rotation happens **before** launch, per invocation.
- cline checks quota via a separate provider API; OpenCode Zen has no usable
  read-only usage endpoint (403 error code 1010) — the only probe is a
  billable `opencode run`, so the script probes lazily, not upfront.

## Troubleshooting

- `No payment method ... /billing` — you pinned a `-nano`/paid model; free models
  are listed in this file above.
- `Model is disabled` on big-pickle — known disabled despite "Free" docs; removed
  from defaults.
- `FreeTierError ... only within OpenCode` on curl — expected; free tier requires
  the CLI. Probe with `opencode run`, never curl.
- `package.json ... Unexpected token` in logs — a shared XDG_CONFIG_HOME between
  accounts corrupted the model cache; give each account its own config dir
  (this script already does) and delete the broken
  `$XDG_CONFIG_HOME/opencode/node_modules`.
- Probe slow (~5-15s each): first call per account/model installs the model's
  npm package into that account's config dir; subsequent runs are faster.

