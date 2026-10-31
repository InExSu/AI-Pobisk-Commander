"""hands.py — when every worker is out, find the supervisor another hand.

Two policies that don't compose:

* a skill's rotate script parks a model after repeated failures and cools it
  down for 15 minutes;
* the supervisor walks its worker list once and then declares the task
  impossible.

A run that hits a provider hiccup at 12:49 gives up while four accounts and
six models are still perfectly usable — just sitting inside a cooldown that
expires in fifteen minutes. That is not a dead fleet, it is a closed door.

So before reporting `exhausted`, ai_Pobisk re-opens the door itself:

* cooldowns are expired (a 15-minute pause is not a verdict);
* `auth_error` stays parked — a bad key does not heal by waiting, and retrying
  it is how a rotation burns its whole budget on a dead account;
* the supervisor's tried-set is cleared, so model rotation starts from the
  top of the catalogue again.

Bounded by `budget.max_rediscoveries`, so a genuinely dead fleet still ends in
`exhausted` instead of spinning.
"""

import json
import os
import time

HEALTH_STORE = os.path.join(
    os.environ.get("AI_ROTATE_DIR") or os.path.expanduser("~/.ai-rotate"),
    "model-stats.json")

# States that mean "transient": worth another attempt once the cooldown ends.
TRANSIENT = ("down", "recovering")
# Never revived here: the key is wrong, waiting will not fix it.
PERMANENT = ("auth_error",)


def revive(store_path, skills, now=None):
    """Expire transient cooldowns for `skills`. Returns {skill: models revived}.

    Only the cooldown is lifted — the failure history stays, so a model that
    keeps dying is still ranked below one that keeps answering. This is not a
    reset: it is "the pause is over, try again".
    """
    now = time.time() if now is None else now
    out = {}
    try:
        with open(store_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out
    if not isinstance(data, dict):
        return out

    changed = False
    for skill in skills:
        models = data.get(skill) or {}
        if not isinstance(models, dict):
            continue
        n = 0
        for _model, e in models.items():
            if not isinstance(e, dict):
                continue
            state = e.get("state")
            if state in PERMANENT:
                continue
            cd = float(e.get("cooldown_until") or 0)
            if state in TRANSIENT and cd > now:
                e["cooldown_until"] = 0
                if state == "down":
                    e["state"] = "recovering"   # eligible, still last-resort
                n += 1
                changed = True
            elif state in TRANSIENT and cd <= now:
                # Already eligible; count it so the caller knows there is a
                # hand to try even though nothing had to be written.
                n += 1
        if n:
            out[skill] = n

    if changed:
        try:
            tmp = store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, store_path)
        except Exception:
            pass
    return out
