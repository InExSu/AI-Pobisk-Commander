---
name: tokenharbor
license: MIT
description: >-
  Ask Token Harbor's free models from the command line. Activates when the user
  says "tokenharbor", "token harbor", "thk_", ":free models", "qwen3.8-flash:free",
  "deepseek-v4.1-flash:free", or asks how to use the models listed at
  https://tokenharbor.ai/models?category=free. There is no CLI client — this
  skill is the client: ask, models, probe, parallel.
---

# Token Harbor — free models from the CLI

`https://tokenharbor.ai/v1` is OpenAI-compatible. No official CLI exists, so
`tokenharbor-rotate.sh` is the client.

## Where the models come from

<https://tokenharbor.ai/models?category=free> is the browsable catalogue. The
machine-readable equivalent is:

```
GET https://tokenharbor.ai/v1/models
Authorization: Bearer thk_live_...
-> {"data":[{"id":"qwen3.8-flash:free","pricing":{"input_usd_per_1m":0,
                                                  "output_usd_per_1m":0}}, ...]}
```

Note the host: **`tokenharbor.ai/v1`**, not `api.tokenharbor.ai` (404) and not
`tokenharbor.ai/api/v1` (404).

## The one non-obvious fact

**Zero pricing alone does not mean free.** `th-orchestra` reports
`input_usd_per_1m: 0, output_usd_per_1m: 0` in the catalogue but returns
**HTTP 402** on a real call. The reliable test is the id suffix **`:free`** —
all four working free models carry it, and `th-orchestra` does not.

So `models --refresh` filters on **both**: zero price AND `:free` suffix.

The four free models (all verified answering, 2026-09-20):

| id | AA rank | Intelligence |
|---|---|---|
| `qwen3.8-flash:free` | #19 | 39.8 |
| `deepseek-v4.1-flash:free` | #22 | 39.5 |
| `deepseek-v4-flash:free` | #32 | 35.0 |
| `mimo-v2.5:free` | #79 | 25.2 |

The site also shows a countdown on some entries ("Free in 00:16:23") — free
windows rotate, so `probe` before relying on a specific id.

## Setup

```bash
python3 .agents/skills/_shared/secrets.py set tokenharbor "thk_live_..."
```

Reads `TOKENHARBOR_API_KEY` from the env, else from `~/.ai-rotate/secrets.json`
(mode 0600). See [`../_shared/SECRETS.md`](../_shared/SECRETS.md).

## Usage

```bash
T=.agents/skills/tokenharbor/tokenharbor-rotate.sh

"$T" models                    # the curated free list
"$T" models --refresh          # re-read the live catalogue, filter on price+suffix
"$T" probe                     # which ones answer right now
"$T" ask "explain this diff"
"$T" ask -m qwen3.8-flash:free "..."
"$T" parallel "prompt 1" "prompt 2" "prompt 3"
```

`ask` auto-picks via the shared health store (stability order).

## Env knobs

| Variable              | Default | Meaning                     |
|-----------------------|---------|-----------------------------|
| `TOKENHARBOR_API_KEY` | store   | key                         |
| `TH_MODELS`           | curated | space-separated ids         |
| `TH_MAX_TOKENS`       | 2048    | max_tokens                  |
| `TH_TIMEOUT`          | 180     | seconds per request         |
| `TH_API_BASE`         | TokenHarbor | override endpoint       |

Exit codes: `0` ok · `1` usage · `2` no API key · `3` all models dead.

Shares `~/.ai-rotate/model-stats.json` with the other skills.

## Gotchas

- **402 on `th-orchestra`** — advertised free, actually not. Excluded.
- **Prompts are argv, not stdin.** The python is fed by a heredoc, so stdin is
  at EOF; reading it would give an empty prompt.
- **Only 4 usable free models** — rotation depth is thin compared to NVIDIA's
  11. `parallel` across all 4 is the way to get throughput.

## Troubleshooting

- `HTTP 402` — that model is not free; check the `:free` suffix.
- `no API key for tokenharbor` — `secrets.py set tokenharbor <key>`.
- All dead — the free window may have rolled; run `models --refresh` and
  `probe` again.
