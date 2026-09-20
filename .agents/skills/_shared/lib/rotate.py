"""rotate.py — the shared rotation decision every supervisor repeats.

Three copies of this logic currently live inside cline-rotate.sh,
opencode-rotate.sh and freebuff-rotate.sh (best-first ordering, cooldown
skipping, family failover). One implementation, imported by all.

Depends only on model-stats.py, which owns the store.
"""

import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.dirname(_HERE)
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# model-stats.py has a hyphen, so it cannot be imported by name. Load it from
# its path instead of renaming the file (skills call it as a script).
def _load_model_stats():
    import importlib.util
    path = os.path.join(_SHARED, "model-stats.py")
    spec = importlib.util.spec_from_file_location("model_stats", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


model_stats = _load_model_stats()  # noqa: E402


def order(skill, models, family_of=None, stats_path=None):
    """Best-first ordering. Returns (ordered, dropped).

    `dropped` maps model id -> reason, for the log. Any failure falls back to
    the caller's order: rotation must never be blocked by the health store.
    """
    models = list(models)
    if not models:
        return [], {}
    if not stats_path or not os.path.exists(stats_path):
        return models, {}
    try:
        args = [sys.executable, stats_path, "order", skill, "--models"] + models
        if family_of:
            args += ["--family-of", family_of]
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return models, {}
        ordered = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        ordered += [m for m in models if m not in ordered]
        dropped = {}
        try:
            st = json.load(open(model_stats.STORE)).get(skill, {})
        except Exception:
            st = {}
        now = time.time()
        for m in models:
            if m in ordered:
                continue
            e = st.get(m) or {}
            if e.get("state") == "auth_error":
                dropped[m] = "auth error, needs a human"
            elif float(e.get("cooldown_until") or 0) > now:
                dropped[m] = "cooling down %ds" % int(
                    float(e["cooldown_until"]) - now)
            else:
                dropped[m] = "excluded by health store"
        return ordered, dropped
    except Exception as e:
        print("  rotate: health order failed: %s" % e, file=sys.stderr)
        return models, {}


def record(skill, model, verdict, latency_ms=None, error="", stats_path=None):
    """Write one probe result. Best-effort: never raises."""
    if not stats_path or not os.path.exists(stats_path):
        return
    try:
        args = [sys.executable, stats_path, "record", skill, model, verdict]
        args.append(str(latency_ms) if latency_ms is not None else "")
        args.append((error or "")[:200])
        subprocess.run(args, capture_output=True, text=True, timeout=20)
    except Exception:
        pass


def elapsed_ms(t0):
    return int((time.time() - t0) * 1000)
