# Shared model-health store

`_shared/model-stats.py` is the one piece of state all three rotate skills
(cline, OpenCode, FreeBuff) share. Before it existed each skill probed
independently and took the **first** model that answered; a model proven broken
by one skill was re-tried from scratch by the next.

Ideas borrowed from [free-coding-models](https://github.com/vava-nessa/free-coding-models)
(FCM, MIT): its Stability Score and per-model circuit breaker. FCM's *router
daemon* is deliberately **not** used — it needs provider API keys and cannot
drive closed CLIs with OAuth sessions. Only the decision logic is taken.

## Store

`~/.ai-rotate/model-stats.json` (override with `AI_ROTATE_DIR`), keyed by skill
then model id:

```
{"<skill>": {"<model id>": {
  "samples": [ms,...],  "p95": ms,      "jitter": ms,   "stability": 0..100,
  "uptime": [ok,total], "fails": n,     "cooldown_until": epoch,
  "state": healthy|down|recovering|auth_error|unknown,
  "last_ok": epoch,     "last_err": ""}}}
```

Corrupt or missing file → treated as empty and rebuilt. Never blocks a run.

## Stability Score (FCM formula, re-capped)

```
0.30 × p95 + 0.30 × jitter + 0.20 × spike_rate + 0.20 × uptime
p95_score    = 100 × (1 - p95 / 8000)      # FCM caps at 5000
jitter_score = 100 × (1 - sigma / 3000)    # FCM caps at 2000
spike_score  = 100 × (1 - spikes / n)      # a probe > 5000ms is a spike
```

Caps are raised because a whole-CLI probe (process start + auth + one ask) is
an order of magnitude slower than FCM's 1-token ping. Weights are unchanged.

The point: average latency lies. A model averaging 250 ms that spikes to 6 s
feels slower than a steady 400 ms one, and is ranked below it.

## Rotation order

`order` returns candidates best-first:

1. **healthy** — proven to answer, sorted by stability desc
2. **unknown** — never tried (or tried but never succeeded): gets a chance
3. **cooling** — broken but cooldown expired: last resort

Dropped entirely: `auth_error`. A bad key needs a human; retrying only burns
the rotation.

Cooldown: 3 consecutive failures → breaker opens for 900 s. Not a daily-quota
reset, just long enough to stop re-probing a dead model on every loop.

## Family-preserving failover

With `--family-of <model>`, siblings of the model that just died are lifted to
the front — a dead `deepseek/*` fails over to another `deepseek/*` instead of
an unrelated model, so mid-session behaviour stays recognisable. Only applies
within already-eligible models: it never revives a dropped or cooling one.

## Usage

```bash
S=.agents/skills/_shared/model-stats.py
python3 "$S" show                          # table: state, stability, p95, uptime
python3 "$S" show cline                    # one skill only
python3 "$S" order cline --models m1 m2    # best-first list
python3 "$S" record cline m1 ok 420        # latency in ms
python3 "$S" record cline m1 auth 0 "bad key"
python3 "$S" reset cline m1                # forget one model
python3 "$S" reset                         # wipe everything
```

Verdicts: `ok` · `fail` (transient/quota) · `auth` (401/403/bad key) · `limit`
(daily cap). Only `auth` parks a model permanently.

## Opting out

`CLINE_NO_STATS=1` / `OC_NO_STATS=1` / `FB_NO_STATS=1` → the skill keeps its
file order and neither reads nor writes the store.

## Invariants

1. **Never blocks.** Any store failure falls back to the caller's own order.
2. **Skill-scoped.** cline and opencode ids never collide; cross-skill benefit
   comes from the shared file, not shared keys.
3. **`order`'s stdout is the list only.** Diagnostics go to stderr, so callers
   can safely do `$(... order ... | head -1)`.
4. **Probing stays non-mutating** — the store is written *after* a probe, never
   instead of one.
