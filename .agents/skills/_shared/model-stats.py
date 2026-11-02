#!/usr/bin/env python3
"""model-stats.py — shared model-health store for every rotate skill.

One file backs all three supervisors (cline / OpenCode / FreeBuff), so a probe
paid by one skill is reused by the others and a model that is down for one is
known to be down for all.

Taken from free-coding-models (https://github.com/vava-nessa/free-coding-models)
which ships a Stability Score and a per-model circuit breaker. Adapted: FCM's
router daemon needs provider API keys and cannot drive closed CLIs with OAuth
sessions, so only the *decision logic* is borrowed, not the daemon.

Store: ~/.ai-rotate/model-stats.json  (override: AI_ROTATE_DIR)
Shape:
    {
      "<skill>": {
        "<model id>": {
          "samples":  [ms, ...],          # rolling, last N successful probes
          "p95":      ms,
          "jitter":   ms,                 # population sigma of samples
          "uptime":   [ok, total],        # successful / all probes
          "stability":0..100,
          "fails":    n,                  # consecutive failures
          "cooldown_until": epoch|0,      # circuit breaker
          "state":    healthy|down|recovering|auth_error|ratelimit|unknown,
          "last_ok":  epoch,
          "last_err": "text",
          "updated":  epoch
        }
      }
    }

Subcommands (all read stdin JSON or take args; every one is side-effect free
except `record`):

    order    <skill> [--models m1 m2 ...] [--account A] [--cooldown N]
                     [--family-of MODEL]
             -> prints model ids, best first: healthy by stability desc, then
                cooling-down by cooldown expiry, then unknown by file order.
                Models in the `down` (auth_error) state are dropped entirely.
                With --family-of, same-family models are lifted to the front:
                a failed deepseek/* falls over to another deepseek/* before
                any other model, so the session keeps the same model character.

    record   <skill> <model> <ok|fail|auth|limit> [latency_ms] [error_text]
             -> updates the store, prints one summary line on stderr.

    repair   -> releases false parks: models parked as `auth_error` whose
                recorded reason does not actually name a credential failure.
                A park bans a model for a day, so a park caused by a provider
                stream error, an empty balance or a timeout removes a working
                model from rotation. Keeps the store honest after a writer
                bug; prints what it released.

    show     [<skill>]   -> human table on stdout.

    reset    [<skill> [<model>]]

Exit codes: 0 ok · 2 bad usage. Never fails on a corrupt store: it is rebuilt.
"""

import json
import math
import os
import sys
import time

STORE_DIR = os.environ.get("AI_ROTATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".ai-rotate"
)
STORE = os.path.join(STORE_DIR, "model-stats.json")

# ── Tunables (FCM defaults, relaxed for CLI agents whose probes are slow) ──────
MAX_SAMPLES = 20            # rolling window of latency samples
COOLDOWN_FAILS = 3          # consecutive failures before the breaker opens
COOLDOWN_SEC = 900          # 15 min: shorter than a daily quota reset, long
                            # enough to stop burning the rotation every loop
# Transient upstream throttle (HTTP 429/520/529, "rate limit", "quota"). The
# model is healthy; the provider is merely rate-limiting. A 15-minute breaker
# is far too long for that — it would sit out an entire run. Park it briefly so
# the fleet routes around a flaky provider for a few minutes, then try again.
RATELIMIT_SEC = 600         # 10 min: a rate-limit window usually clears sooner
AUTH_PARK_SEC = 86400      # 24h: how long a bad-credential model stays
                            # parked. Finite on purpose — a permanent park
                            # made rotation collapse to one model unnoticed.
                            # After the expiry it is retried once.
AUTH_MAX_PARKS = 3         # parked this many times -> stop retrying for good
P95_CAP_MS = 8000           # probes are whole CLI asks, not 1-token pings
JITTER_CAP_MS = 3000
SPIKE_MS = 5000             # a probe above this counts as a spike
RECENT_TTL = 0              # 0 = never expire samples on age alone


def load():
    try:
        with open(STORE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(d):
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2, sort_keys=True)
        os.replace(tmp, STORE)
    except Exception as e:
        print("model-stats: could not save %s: %s" % (STORE, e), file=sys.stderr)


def blank():
    return {
        "samples": [], "p95": 0, "jitter": 0, "uptime": [0, 0],
        "stability": 0, "fails": 0, "cooldown_until": 0, "parks": 0,
        "state": "unknown", "last_ok": 0, "last_err": "", "updated": 0,
    }


def percentile(sorted_vals, p):
    """Nearest-rank p-percentile; no numpy, no deps."""
    if not sorted_vals:
        return 0
    k = max(0, math.ceil(p / 100.0 * len(sorted_vals)) - 1)
    return sorted_vals[min(k, len(sorted_vals) - 1)]


def sigma(vals):
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / float(len(vals))
    return math.sqrt(sum((v - m) ** 2 for v in vals) / float(len(vals)))


def stability(e):
    """FCM's composite, re-weighted for CLI probes.

    FCM: 30% p95 + 30% jitter + 20% spike rate + 20% reliability, with p95
    capped at 5000ms and jitter at 2000ms. Whole-CLI probes (process start +
    auth + one ask) are an order of magnitude slower than FCM's 1-token pings,
    so the caps are raised here; the weights are unchanged.
    """
    s = sorted(e.get("samples") or [])
    ok, total = e.get("uptime") or [0, 0]

    p95_raw = percentile(s, 95) if s else 0
    jit_raw = sigma(s)
    spikes = sum(1 for v in s if v > SPIKE_MS)
    uptime = (100.0 * ok / total) if total else 0.0

    p95_score = 100.0 * (1 - p95_raw / float(P95_CAP_MS))
    jitter_score = 100.0 * (1 - jit_raw / float(JITTER_CAP_MS))
    spike_score = 100.0 * (1 - spikes / float(len(s))) if s else 0.0

    clamp = lambda x: max(0.0, min(100.0, x))
    return round(
        0.30 * clamp(p95_score)
        + 0.30 * clamp(jitter_score)
        + 0.20 * clamp(spike_score)
        + 0.20 * clamp(uptime),
        1,
    )


def refresh(e):
    s = sorted(e.get("samples") or [])
    e["p95"] = round(percentile(s, 95), 1) if s else 0
    e["jitter"] = round(sigma(s), 1)
    e["stability"] = stability(e)
    return e


# ── Model families (FCM's family-preserving failover) ─────────────────────────
# When a model dies mid-session, jump to the same family on another provider
# before falling back to plain priority order: the assistant keeps the same
# behaviour instead of silently switching model character.
FAMILIES = [
    ("deepseek", "deepseek"), ("glm", "glm"), ("qwen", "qwen"),
    ("kimi", "kimi"), ("nemotron", "nemotron"), ("llama", "llama"),
    ("mimo", "mimo"), ("solar", "solar"), ("gemini", "gemini"),
    ("gpt", "gpt"), ("claude", "claude"), ("mistral", "mistral"),
    ("muse", "muse"), ("ling", "ling"), ("dots", "dots"),
    ("laguna", "laguna"), ("inkling", "inkling"), ("north", "north"),
    ("step", "step"), ("jev", "jev"), ("lfm", "lfm"), ("nex", "nex"),
]


def family(model):
    low = (model or "").lower()
    for needle, name in FAMILIES:
        if needle in low:
            return name
    return ""


# ── record ────────────────────────────────────────────────────────────────────
def cmd_record(argv):
    if len(argv) < 3:
        print("usage: model-stats.py record <skill> <model> "
              "<ok|fail|auth|limit> [latency_ms] [error]", file=sys.stderr)
        return 2
    skill, model, verdict = argv[0], argv[1], argv[2]
    latency = None
    err = ""
    if len(argv) >= 4 and argv[3] not in ("", "-"):
        try:
            latency = float(argv[3])
        except ValueError:
            pass
    if len(argv) >= 5:
        err = argv[4]

    d = load()
    e = d.setdefault(skill, {}).get(model) or blank()
    ok, total = e.get("uptime") or [0, 0]
    now = time.time()

    if verdict == "ok":
        e["fails"] = 0
        e["cooldown_until"] = 0
        e["state"] = "healthy"
        e["last_ok"] = now
        e["last_err"] = ""
        ok += 1
        total += 1
        if latency is not None:
            e["samples"] = (e.get("samples") or [])[-MAX_SAMPLES:] + [latency]
    else:
        total += 1
        e["last_err"] = (err or verdict)[:200]
        if verdict == "auth":
            # Bad credentials do not heal by retrying, so they are parked
            # rather than retried. But NOT forever: accounts get re-authed,
            # keys get rotated, and a permanent park silently collapses
            # rotation depth (19 of cline's 20 models ended up parked once,
            # from failures that were actually the account's, not the
            # model's). Park with an expiry, then let it be tried again.
            e["state"] = "auth_error"
            e["cooldown_until"] = now + AUTH_PARK_SEC
            e["parks"] = (e.get("parks") or 0) + 1
        elif verdict == "limit":
            # Transient upstream throttle (429/520/529, "rate limit",
            # "quota"). The model is fine, the provider is merely limiting;
            # route around it for a few minutes instead of hammering it or
            # counting it toward the hard-failure breaker.
            e["state"] = "ratelimit"
            e["cooldown_until"] = now + RATELIMIT_SEC
        else:
            e["fails"] = (e.get("fails") or 0) + 1
            if e["fails"] >= COOLDOWN_FAILS:
                e["state"] = "down"
                e["cooldown_until"] = now + COOLDOWN_SEC
            else:
                e["state"] = "recovering"

    e["uptime"] = [ok, total]
    e["updated"] = now
    refresh(e)
    d.setdefault(skill, {})[model] = e
    save(d)

    print("  stats  %s %s: %s stab=%s p95=%sms up=%s/%s fails=%s" % (
        skill, model, e["state"], e["stability"], e["p95"],
        e["uptime"][0], e["uptime"][1], e["fails"]), file=sys.stderr)
    return 0


# ── order ─────────────────────────────────────────────────────────────────────
def cmd_order(argv):
    """Best-first ordering of the candidate models for one rotation pass."""
    skill = None
    models_input = []
    cooldown = COOLDOWN_SEC
    family_of = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--models":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                models_input.append(argv[i])
                i += 1
            continue
        if a == "--cooldown":
            i += 1
            cooldown = float(argv[i])
            i += 1
            continue
        if a == "--family-of":
            i += 1
            family_of = argv[i]
            i += 1
            continue
        if a == "--account":      # accepted for forward compat, unused
            i += 2
            continue
        if skill is None:
            skill = a
        i += 1
    if not skill:
        print("usage: model-stats.py order <skill> --models m1 m2 ... "
              "[--cooldown N]", file=sys.stderr)
        return 2
    if not models_input:
        return 0

    # If --models not given, read from store or use defaults
    d = load().get(skill, {})
    now = time.time()

    # Build the model list: use --models if given, otherwise derive from store
    # or default models. Models not in the store are treated as unknown.
    if models_input:
        models = models_input
    else:
        # Use default models from the skill's perspective, or those in the store
        models = [m for m in d.keys()] if d else []
        if not models:
            print("no models found in store for skill %s" % skill, file=sys.stderr)
            return 0

    healthy, cooling, unknown = [], [], []

    for rank, m in enumerate(models):
        e = d.get(m)
        if e is None:
            # Model never probed: give it a chance at the end
            unknown.append((rank, 0.0, m))
            continue
        cd = float(e.get("cooldown_until") or 0)
        state = e.get("state")

        # 1. Auth-parked models: skip while parked, give one chance after
        #    expiry, unless maxed out parks (then genuinely broken -> the
        #    credential can no longer be fixed by waiting, so stop asking).
        if state == "auth_error":
            if cd > now:
                # Still parked -> excluded from THIS pass entirely. Rotation
                # picks it up only when the park expires, never as a tail
                # entry: a parked model returned last is still returned.
                continue
            if (e.get("parks") or 0) >= AUTH_MAX_PARKS:
                print("  stats  %s/%s skipped: %d parks reached, "
                      "likely broken credential" % (skill, m, e.get("parks", 0)),
                      file=sys.stderr)
                continue
            # Park expired, promote to recovering for one more chance
            e["state"] = "recovering"

        # 2. Transient states (down, ratelimit) still cooling -> defer
        if state in ("down", "ratelimit") and cd > now:
            cooling.append((cd, rank, m))
            continue

        # 3. Transient states that have cooled -> recovery phase (one retry)
        if state in ("down", "ratelimit") and cd <= now:
            e["state"] = "recovering"

        # 4. Models with recorded successes -> rank by stability
        if (e.get("uptime") or [0, 0])[0] > 0:
            healthy.append((-(e.get("stability") or 0), rank, m))
        else:
            # Proven never succeeded (0/0 or 0/N): give a chance, but last
            unknown.append((rank, 0.0, m))

    # healthy (best stability first) → unknown (never tried) → cooling last
    healthy.sort(key=lambda x: x[0])
    cooling.sort(key=lambda x: x[0])
    unknown.sort(key=lambda x: x[0])

    ordered = [m for _, _, m in healthy] + \
              [m for _, _, m in unknown] + \
              [m for _, _, m in cooling]

    # Family-preserving failover: after `family_of` died, prefer its siblings.
    if family_of:
        fam = family(family_of)
        if fam:
            kin = [m for m in ordered
                   if m != family_of and family(m) == fam]
            if kin:
                ordered = kin + [m for m in ordered if m not in kin]
                print("  stats  family hop: %s -> %s (%d sibling(s) first)" % (
                    family_of, fam, len(kin)), file=sys.stderr)

    print("\n".join(ordered))
    return 0


# ── repair ───────────────────────────────────────────────────────────────────
# A park means "this credential is broken" and bans the model for a day. The
# rotate scripts used to park a model whenever a probe failed for ANY reason
# — a provider stream error, an empty balance, a timeout. Those parks removed
# working models from rotation: 8 of cline's 13 models sat in auth_error,
# including one with 14 recorded successes, and the fleet collapsed onto a
# single model.
#
# The repair keeps a park only when the recorded reason actually names a
# credential failure. Everything else is released. These are the same markers
# the Python side uses (adapters/base.py::_is_credential_failure) so the two
# halves of the system agree on what "broken credential" means.
CREDENTIAL_MARKERS = (
    "credential rejected", "auth error", "invalid api key", "unauthorized",
    "unauthenticated", "no authtoken", "no api key", "missing api key",
    "not authenticated", "key has expired", "401", "403",
)


def is_credential_reason(text):
    """True when the recorded reason names a broken credential."""
    low = (text or "").lower()
    return any(m in low for m in CREDENTIAL_MARKERS)


def cmd_repair(argv):
    """Release parks that were not caused by a broken credential."""
    d = load()
    released = 0
    for skill, models in list((d or {}).items()):
        if not isinstance(models, dict):
            continue
        for mid, e in list(models.items()):
            if not isinstance(e, dict) or e.get("state") != "auth_error":
                continue
            if is_credential_reason(e.get("last_err")):
                continue          # a genuine credential failure: keep it
            e["state"] = "recovering"
            e["cooldown_until"] = 0
            e["fails"] = 0
            # The park counter decides whether the model is dropped for good
            # (AUTH_MAX_PARKS). A false park must not spend that budget.
            e["parks"] = 0
            released += 1
            print("  repair  %s/%s released (park reason was not a "
                  "credential failure)" % (skill, mid), file=sys.stderr)
    if released:
        save(d)
    print("released %d false park(s)" % released)
    return 0


# ── show / reset ──────────────────────────────────────────────────────────────
def cmd_show(argv):
    d = load()
    skills = [argv[0]] if argv and argv[0] in d else sorted(d)
    if not d:
        print("no stats yet (%s)" % STORE)
        return 0
    print("%-9s %-42s %-11s %5s %8s %8s %7s %s" % (
        "skill", "model", "state", "stab", "p95(ms)", "jitter", "up", "fails"))
    for sk in skills:
        for m, e in sorted(d.get(sk, {}).items(),
                           key=lambda kv: -(kv[1].get("stability") or 0)):
            ok, total = e.get("uptime") or [0, 0]
            cd = e.get("cooldown_until") or 0
            extra = ""
            if cd > time.time():
                extra = " cool %ds" % int(cd - time.time())
            print("%-9s %-42s %-11s %5s %8s %8s %5s/%s %s%s" % (
                sk, m[:42], e.get("state", "?"), e.get("stability", 0),
                e.get("p95", 0), e.get("jitter", 0), ok, total,
                e.get("fails", 0), extra))
    return 0


def cmd_reset(argv):
    d = load()
    if not argv:
        save({})
    elif len(argv) == 1:
        d.pop(argv[0], None)
        save(d)
    else:
        d.get(argv[0], {}).pop(argv[1], None)
        save(d)
    print("reset ok (%s)" % STORE)
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, argv = sys.argv[1], sys.argv[2:]
    fn = {"record": cmd_record, "order": cmd_order,
          "show": cmd_show, "reset": cmd_reset,
          "repair": cmd_repair}.get(cmd)
    if fn is None:
        print("model-stats: unknown subcommand %r" % cmd, file=sys.stderr)
        return 2
    return fn(argv)


if __name__ == "__main__":
    sys.exit(main())
