# API keys — one store, outside git

Every skill reads its API keys from a single file:

```
~/.ai-rotate/secrets.json      mode 0600    (never inside the repo)
```

Managed by `_shared/secrets.py`. Nothing in this repository contains a key, and
`secrets.py check` fails loudly if one ever appears there.

## Quick start

```bash
S=.agents/skills/_shared/secrets.py

python3 "$S" set nvidia      "nvapi-..." --label "NVIDIA NIM"
python3 "$S" set tokenharbor "thk_live_..." 
python3 "$S" list                  # masked: nvap…-ddy (70 chars)
python3 "$S" get nvidia            # raw value, for scripting
python3 "$S" check                 # perms + scan the repo for stray keys
python3 "$S" fix-perms             # chmod 600 after an accidental chmod 644
python3 "$S" rm baibai             # delete
```

## How scripts read a key

Never paste a key into a script. Source the helper and ask by name:

```bash
. "$(dirname "$0")/../_shared/require-secret.sh"
require_secrets NVIDIA_API_KEY:nvidia     # exits 2 with a hint if missing
```

- An already-exported variable wins, so CI and wrappers can override.
- `require_secret VAR NAME` returns non-zero instead of exiting, if you want to
  handle the miss yourself.
- Keys are never echoed: `require-secret` prints nothing on success.

Env var names are derived per secret: `nvidia -> NVIDIA_API_KEY`,
`tokenharbor -> TOKENHARBOR_API_KEY`. `python3 secrets.py export` prints them all.

## Stored keys

| Name | Env var | Used by |
|---|---|---|
| `nvidia` | `NVIDIA_API_KEY` | `NVIDIA/nvidia-rotate.sh` |
| `tokenharbor` | `TOKENHARBOR_API_KEY` | `TokenHarbor/tokenharbor-rotate.sh` |
| `baibai` | `BAI_API_KEY` | was the Qwen Code provider (api.b.ai) |

## Rules

1. **0600 or nothing.** The store refuses to read a file that group/other can
   read — it exits 2 and tells you to run `fix-perms`, rather than silently
   handing over keys from a world-readable file.
2. **Masked by default.** `list` shows first-4 + last-4 + length. Only
   `get`/`reveal` print a full key.
3. **Outside git.** The store lives in `~/.ai-rotate/`, never in the repo. The
   repo's `.gitignore` also excludes `*.env`, `secrets*`, and `keys.txt`.
4. **`secrets.py check` scans the working tree** for `nvapi-`, `thk_live_`,
   `sk-`, `sk-ant-`, `gsk_`, `AIza` inside tracked file types and reports the
   paths, so a key accidentally committed to a script gets caught.

## Rotating a key

```bash
python3 "$S" set nvidia "nvapi-NEW..."    # overwrites, keeps `added`
```

Skills read the key at every invocation, so no restart or re-auth is needed.
