---
name: freebuff
license: MIT
description: >-
  Launch the Freebuff CLI (https://freebuff.com) on an account that still has
  daily quota, so it never opens straight into "Daily Freebuff limit reached".
  Activates when the user says "freebuff", "запусти freebuff", "freebuffROTATE",
  "кончились лимиты freebuff", "Daily Freebuff limit reached", "смени аккаунт
  freebuff", "freebuff quota", or when a freebuff session dies with the daily /
  weekly cap banner. Probes every saved account's quota over the read-only
  codebuff session API, restores the best account, pins a model with remaining
  quota, then hands the terminal over. Do NOT rotate freebuff accounts for
  codebuff or non-freebuff agents.
---

# FreeBuff — quota-aware Freebuff CLI launcher

`freebuff` (the free Codebuff tier) runs an interactive TUI only: **no
non-interactive prompt mode, no `-m` model flag, and no `--data-dir`**. All
accounts share one config root, `~/.config/manicode/`. When the daily sessions
run out, the TUI dies with:

```
☕ Daily Freebuff limit reached
```

The CLI has **no built-in account rotation**, so an unattended run stops there.
This skill wraps `freebuff` in a supervisor that, before every launch, checks
every saved account over the network and launches on one that still has quota.

## The one non-obvious fact (read this first)

Quota is checked with a **read-only HTTP GET**, not by running freebuff. The
endpoint — the same one the fbacc TUI's Quota view uses — is:

```
GET https://codebuff.com/api/v1/freebuff/session
Authorization: Bearer <authToken from the saved account file>
```

It returns two independent gates, and a model is launchable only when **both**
pass:

1. **`rateLimitsByModel`** — per model `recentCount`, `limit`, `resetAt`
   (pacific-day window, resets ~07:00 UTC). Metered sessions.
2. **`freebucks`** — the currency **every** model burns per request: `balance`,
   `daily` (`limit`/`spent`/`remaining`), and `prices` (GLM 5, MiMo/Solar 10,
   DeepSeek 15, Luna 20, Gemini 50 Freebucks per request). An account with
   `0 Freebucks left` cannot run **any** model, even with full session quota —
   the TUI dies with "Not enough Freebucks — 5 Freebucks/hr against 0 left".

It never burns a session, so unlike the cline skill there is no throwaway
probing and no text-vs-exit-code ambiguity. Never "replace" this probe by
launching freebuff to "test" an account: that consumes real Freebucks.

Account switching has no isolation mechanism: **restoring an account means
overwriting `~/.config/manicode/credentials.json`** — exactly what fbacc's
"Restore account" does, same JSON shape (`{"default": {...}}`), including
`fingerprintId`/`fingerprintHash`. Two consequences:

1. The active session is backed up to `credentials.json.bak` on every swap, so
   a session not present in the accounts dir is never lost.
2. `codebuff` shares the same `credentials.json` — a swap changes which account
   codebuff runs on too. The supervisor warns if a freebuff instance is already
   alive (`freebuff-instance-owner.json` holds its pid).

## Layout

```
<repo>/.agents/skills/freebuff/
├── SKILL.md                     ← this file
├── freebuff-rotate.sh           ← the supervisor (bash, no deps beyond freebuff + python3)
├── FreeBuff_Models_Free_Make.sh ← regenerates the catalogue below from the session API
└── freebuff-models-free.txt     ← every free-tier model id, one per line (generated)
```

Model choice is quota-driven, but ties are broken through the shared health
store: `~/.ai-rotate/model-stats.json` via `../_shared/model-stats.py`, so a
model another skill proved broken is skipped. See
[`../_shared/README.md`](../_shared/README.md). Disable with `FB_NO_STATS=1`.

`FreeBuff_Models_Free_Make.sh` is the analogue of `cline_Models_Free_Make.sh` /
`opencode_Models_Free_Make.sh`. Unlike those two there is no local binary to
scan: freebuff has no `freebuff models` command, so the script reads the same
read-only session endpoint as the probe and unions `freebucks.prices` with
`rateLimitsByModel` across every saved account (cheapest first). Re-run it when
the free tier changes:

```bash
"$RUNNER_DIR/FreeBuff_Models_Free_Make.sh"     # rewrites freebuff-models-free.txt
```

## Resolve the runner for this session

```bash
resolve_runner() {
  local d="$PWD"
  while [[ "$d" != "/" ]]; do
    [[ -x "$d/.agents/skills/freebuff/freebuff-rotate.sh" ]] && { echo "$d/.agents/skills/freebuff/freebuff-rotate.sh"; return 0; }
    d="$(dirname "$d")"
  done
  return 1
}
RUNNER="$(resolve_runner)"
```

## What it does on launch

1. **Probe** every account in `$FB_ACCOUNTS_DIR` (default
   `~/.config/manicode/accounts`) over the session API. 403 = blocked, other
   errors are reported per-account and that account is skipped.
2. **Select** the (account, model) pair: prefer models in `FB_MODELS` order;
   a model must fit **both** gates — its session quota not exhausted (when
   metered) and its Freebucks price covered by the account balance. On a tie
   the account with more Freebucks left wins.
3. **Restore** the winning account into `credentials.json` (previous active
   copy kept as `.bak`) and **pin** the model into `settings.json`
   (`freebuffModel`), atomically.
4. **exec freebuff --trust-agents** (added automatically, unless the caller
   passes it explicitly; `--continue` resumes the last conversation) — the
   interactive TUI takes over the process.

If the session hits the cap mid-run, quit and re-run the supervisor — the probe
then routes to the next account. There is no mid-run rotation: freebuff cannot
be restarted headlessly from inside the TUI.

## Accounts

Accounts are the JSON files fbacc saves under
`~/.config/manicode/accounts/` (`_index.json` excluded). An account joins the
rotation automatically — no config edit. Add one:

```bash
freebuff login            # log in once in the TUI
fbacc                     # TUI -> "Extract session", give it a name
```

or copy any fbacc-style account file (with `authToken`, `email`,
`fingerprintId`, `fingerprintHash`) into the accounts dir.

## Inspecting state

```bash
"$RUNNER" quota                       # accounts x models table, read-only
tail -n 40 ~/.config/manicode/logs/freebuff-rotate.log

# which account is active right now
python3 -c 'import json,os; print(json.load(open(os.path.expanduser(
  "~/.config/manicode/credentials.json")))["default"]["email"])'
```

## Env knobs

| Variable         | Default                    | Meaning                          |
|------------------|----------------------------|----------------------------------|
| `FB_MANICODE_DIR`| `~/.config/manicode`       | config root                      |
| `FB_ACCOUNTS_DIR`| `$FB_MANICODE_DIR/accounts`| saved accounts root              |
| `FB_BIN`         | `freebuff`                 | binary to exec                   |
| `FB_MODELS`      | glm → deepseek → mimo → solar | priority model ids            |
| `FB_LOG_DIR`     | `$FB_MANICODE_DIR/logs`    | where freebuff-rotate.log lives  |
| `FB_PROBE_TIMEOUT`| 10                        | seconds per HTTP probe           |

## Loop-safety invariants (do not break these)

1. **Probing must never mutate state** — it is a plain GET; only the winning
   account is written into `credentials.json` (previous copy to `.bak`).
2. **Detection is by data, not exit codes** — quota comes from the API response;
   a blocked account (403) is skipped, never retried.
3. **A single mutation point** — restore + settings pin happen once, only for
   the selected pair, then the supervisor `exec`s into the TUI.
4. **Exhaustion is a hard exit (5)** — no sleep-and-retry loop; quotas reset at
   the pacific-day boundary (~07:00 UTC).

## Testing without touching the real config

Point everything at a temp copy — the supervisor never needs the real
`~/.config/manicode` to be exercised:

```bash
TMP=$(mktemp -d)
cp -r ~/.config/manicode/accounts "$TMP"/
cp ~/.config/manicode/settings.json "$TMP"/ 2>/dev/null
FB_MANICODE_DIR="$TMP" FB_BIN=/bin/echo "$RUNNER"      # dry launch: prints the exec line
FB_MANICODE_DIR="$TMP" "$RUNNER" quota                 # read-only table
rm -rf "$TMP"
```

## Troubleshooting

- **`Not enough Freebucks` in the TUI** — the selected account had Freebucks
  but ran out mid-session (each request costs the model's price: GLM 5,
  DeepSeek 15, …). Quit and re-run the supervisor; it will switch accounts.
- **`all accounts exhausted or blocked`** — every saved account is out of
  Freebucks and/or sessions until reset (~21:00 UTC = local midnight). Add an
  account (`fbacc` → Extract session) or wait.
- **Account skipped with `no authToken` / `HTTP 4xx`** — its saved token died
  or the file is malformed; re-extract the session with fbacc.
- **`BLOCKED (403)`** — the account is rate-blocked server-side; do not retry it.
- **Model mismatch warnings in the TUI** — the session was started on another
  model; the supervisor re-pins `freebuffModel` on every launch.
- **`credentials.json.bak` divergence** — that is the account that was active
  before the last swap; restore it by copying it back, or save it via fbacc.

