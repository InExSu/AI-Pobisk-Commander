"""logrot.py — keep journals and skill logs from growing without bound.

Nothing rotated anything before: journal.md, state.json notes, the per-skill
rotate logs and model-stats.json all grew forever. On a long-running
supervisor that means a journal you can no longer read and a health store
whose `samples` list only ever gets longer.

Policy: rotate by SIZE, keep N numbered copies, newest first
(journal.md -> journal.1.md -> journal.2.md ...). Simple, predictable, and
works without cron or external tools.
"""

import os
import shutil


def rotate(path, max_bytes=1_000_000, keep=3):
    """Rotate `path` if larger than max_bytes. Returns the new name or None.

    keep=0 means "truncate in place" instead of keeping copies.
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return None
    except OSError:
        return None

    if keep <= 0:
        try:
            open(path, "w").close()
            return path
        except OSError:
            return None

    # Shift the existing copies up: .2 -> .3, .1 -> .2, then file -> .1
    for i in range(keep, 0, -1):
        old = "%s.%d" % (path, i)
        if not os.path.exists(old):
            continue
        if i >= keep:
            try:
                os.remove(old)
            except OSError:
                pass
            continue
        try:
            shutil.move(old, "%s.%d" % (path, i + 1))
        except OSError:
            pass
    try:
        shutil.move(path, "%s.1" % path)
    except OSError:
        return None
    return path


def rotate_all(repo_root, cfg, extra_paths=()):
    """Rotate everything ai_Pobisk owns. Best-effort: never raises."""
    lg = cfg.get("logs") or {}
    max_bytes = int(lg.get("max_bytes", 1_000_000))
    keep = int(lg.get("keep", 3))
    jmax = int(lg.get("journal_max_bytes", 2_000_000))

    paths = list(extra_paths)
    st = cfg.get("state") or {}
    d = os.path.join(repo_root, st.get("dir", ".ai_pobisk"))
    if st.get("journal"):
        paths.append(os.path.join(d, st["journal"]))

    # Skill logs live outside the repo; rotate the ones we know about.
    home = os.path.expanduser("~")
    for rel in (".cline/logs/cline-rotate.log",
                ".config/manicode/logs/freebuff-rotate.log",
                ".qwen-accounts/../logs/qwen-rotate.log"):
        p = os.path.join(home, rel)
        if os.path.exists(p):
            paths.append(p)

    done = []
    for p in paths:
        try:
            cap = jmax if p.endswith(".md") else max_bytes
            if rotate(p, max_bytes=cap, keep=keep):
                done.append(p)
        except Exception:
            continue
    return done


def sweep_stats(store_path, max_parks=3, unknown_max_age=7 * 86400):
    """Drop health entries that can never be useful again.

    Two kinds of junk accumulate in the store and quietly shrink rotation:
      * models parked past the retry limit (`parks >= max_parks`) — they are
        excluded from ordering anyway, they just keep inflating the count and
        tripping the "parked models" alert;
      * entries that were never once successful and have not been touched in
        `unknown_max_age` — leftovers from experiments and one-off failures.
    """
    import json
    if not os.path.exists(store_path):
        return 0
    try:
        with open(store_path) as f:
            d = json.load(f)
    except Exception:
        return 0
    import time
    now = time.time()
    dropped = 0
    for skill, models in (d or {}).items():
        if not isinstance(models, dict):
            continue
        for mid in list(models):
            e = models[mid] or {}
            too_parked = (e.get("parks") or 0) >= max_parks
            stale = ((e.get("uptime") or [0, 0])[0] == 0
                     and (now - (e.get("updated") or 0)) > unknown_max_age)
            if too_parked or stale:
                models.pop(mid, None)
                dropped += 1
    if dropped:
        try:
            tmp = store_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=2, sort_keys=True)
            os.replace(tmp, store_path)
        except OSError:
            return 0
    return dropped


def trim_stats(store_path, max_samples=20, max_models_per_skill=200):
    """Cap the health store: sample lists and model count per skill.

    `max_samples` matches model-stats.py's own rolling window — trimming here
    is a safety net for stores written by older versions.
    """
    if not os.path.exists(store_path):
        return False
    import json
    try:
        with open(store_path) as f:
            d = json.load(f)
    except Exception:
        return False
    changed = False
    for skill, models in (d or {}).items():
        if not isinstance(models, dict):
            continue
        if len(models) > max_models_per_skill:
            # Drop the least recently updated.
            order = sorted(models.items(),
                           key=lambda kv: kv[1].get("updated") or 0)
            for k, _ in order[:len(models) - max_models_per_skill]:
                models.pop(k, None)
                changed = True
        for m in models.values():
            s = m.get("samples") or []
            if len(s) > max_samples:
                m["samples"] = s[-max_samples:]
                changed = True
    if changed:
        try:
            tmp = store_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=2, sort_keys=True)
            os.replace(tmp, store_path)
        except OSError:
            return False
    return changed
