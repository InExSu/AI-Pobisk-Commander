---
name: nvidia
license: MIT
description: >-
  Ask NVIDIA NIM's free models from the command line. Activates when the user
  says "nvidia", "NIM", "nvidia models", "build.nvidia.com", "nvidia free
  models", "запусти nvidia", or wants to fan prompts out across many free models
  at once. There is no official NVIDIA CLI — this skill is the CLI: ask, models,
  probe, parallel, rpm. Key fact: NIM rate-limits 40 requests/minute PER MODEL,
  so spreading work across N models yields N x 40 RPM.
---

# NVIDIA NIM — free models from the CLI

`https://integrate.api.nvidia.com/v1` is OpenAI-compatible. There is **no
official CLI client**, so `nvidia-rotate.sh` is the client.

## The one non-obvious fact (read this first)

**The rate limit is 40 requests/minute PER MODEL, not per key.**

```
1 model  ->  40 RPM
9 models -> 360 RPM   (same key, no rule broken)
11 models -> 440 RPM
```

Two consequences:

1. **Serial rotation wastes capacity.** Trying models one after another on
   failure uses one model's budget at a time. `--parallel` spreads N prompts
   across N distinct ids, and each costs 1 request against its own 40 RPM.
2. **Retries should switch model, not repeat it.** Hitting the same id again
   immediately after a 429 just burns that model's budget.

Response headers carry **no** rate-limit info (`x-ratelimit-*` absent), so the
40/model figure is from NVIDIA's docs — treat it as the design constant.

## Setup

```bash
python3 .agents/skills/_shared/secrets.py set nvidia "nvapi-..."
```

The script reads `NVIDIA_API_KEY` from the env, else from the secret store
(`~/.ai-rotate/secrets.json`, mode 0600). See
[`../_shared/SECRETS.md`](../_shared/SECRETS.md).

## Usage

```bash
N=.agents/skills/nvidia/nvidia-rotate.sh

"$N" models                                  # catalogue (curated, verified)
"$N" probe                                   # which models answer right now
"$N" rpm                                     # the per-model budget

"$N" ask "explain this diff"
"$N" ask -m z-ai/glm-5.3-flash "..."         # pin a model

# the point of the skill: N prompts, N models, one request each
"$N" parallel "What is 2+2?" "Capital of France?" "Name a prime." "..."
```

`ask` auto-picks via the shared health store (stability order). `probe` runs in
parallel — per-model limits make that safe and much faster than serial.

## The catalogue is a trap

`GET /v1/models` returns **82 ids**. Most do not work:

- **41 return HTTP 404** `Function '<uuid>' not found` — listed but not deployed
  on the free tier.
- **Some are just slow.** `z-ai/glm-5.3-flash` and `moonshotai/kimi-k3` take
  60-130 s on a cold call and look dead under a short timeout. Always give them
  `NVIDIA_TIMEOUT` >= 240 s before concluding they are broken.
- Intermittent **503 "Service temporarily overloaded"** is common and transient.

The 11 ids in `DEFAULT_MODELS` are the ones that answered a real completion
during the 2026-09-20 audit. Re-check with `probe` — the deployed set changes.

Best of the pool by SWE-bench: `z-ai/glm-5.3-flash` (S+, 71.9% on nemotron-3-ultra),
`moonshotai/kimi-k3`, `nvidia/nemotron-3-ultra-550b-a55b` (S+, 71.9%, 1M ctx).

## Env knobs

| Variable             | Default | Meaning                          |
|----------------------|---------|----------------------------------|
| `NVIDIA_API_KEY`     | store   | key                              |
| `NVIDIA_MODELS`      | curated | space-separated model ids        |
| `NVIDIA_RPM_PER_MODEL`| 40     | per-model budget (docs value)    |
| `NVIDIA_MAX_TOKENS`  | 2048    | max_tokens per answer            |
| `NVIDIA_TIMEOUT`     | 240     | seconds; raise for glm/kimi      |
| `NVIDIA_API_BASE`    | NVIDIA  | override the endpoint            |
| `AI_ROTATE_DIR`      | `~/.ai-rotate` | secret + health store      |

Exit codes: `0` ok · `1` usage · `2` no API key · `3` all models dead.

Shares `~/.ai-rotate/model-stats.json` with the other skills, so a model that
just failed here is deprioritised everywhere.

## Gotchas

- **Empty prompt bug (fixed, do not reintroduce).** The python is fed by a
  heredoc, so stdin is at EOF — reading the prompt from stdin yields `""` and
  models reply "your message came through empty". Prompts are passed as argv.
- **Reasoning models put the answer in `reasoning_content`**, leaving `content`
  null when the token budget is tight. The client reads both.
- **`parallel` takes prompts as arguments**, not stdin (same heredoc reason), or
  from `$NVIDIA_PROMPTS_FILE`.
- **Responses are slow** (2-130 s). Budget accordingly; do not assume a 10 s
  timeout is "long enough".

## Troubleshooting

- `HTTP 404 Function not found` — model not on the free tier. Run `probe` and
  drop it from the list.
- `HTTP 503 Service temporarily overloaded` — transient; retry or switch model.
- `TimeoutError` on glm/kimi — raise `NVIDIA_TIMEOUT`; they are slow, not dead.
- `no API key for nvidia` — `secrets.py set nvidia <key>`, or export
  `NVIDIA_API_KEY`.
