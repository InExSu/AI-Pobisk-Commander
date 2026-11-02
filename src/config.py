"""config.py — load configs/ai_pobisk.toml, with sane defaults if absent.

Zero dependencies: `tomllib` is stdlib since 3.11.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    tomllib = None

DEFAULTS = {
    "meta": {
        "max_tokens": 512,
        "timeout_sec": 90,
        "require_json": True,
        "max_reparse": 1,
        "candidates": [],
    },
    "fallback": {"on_meta_unavailable": "rules_only",
                 "fatal_confidence_floor": 0.7},
    "budget": {
        "max_retries_per_step": 3,
        "max_model_rotations": 10,
        "max_account_rotations": 5,
        "max_meta_calls_per_task": 40,
        "max_wall_clock_sec": 3600,
        "max_steps_per_task": 50,
        "step_timeout_sec": 1800,
        "min_step_ms": 2000,
        "max_nudges_per_step": 5,
        "max_iterations_per_step": 40,
        "max_no_evidence_per_step": 3,
        # Times the supervisor may lift transient cooldowns and walk the whole
        # worker list again before accepting that there are no hands left.
        "max_rediscoveries": 2,
    },
    # Order is by CAPABILITY, not by reliability. cline/opencode run a real
    # agent with file tools and can do the work; nvidia/tokenharbor are bare
    # completion calls with no filesystem, so they come last and are dropped
    # entirely for steps that edit files.
    "workers": {"order": ["cline", "opencode", "nvidia", "tokenharbor"],
                "non_workers": ["freebuff"],
                # Workers with no filesystem: a completion call cannot read or
                # write the repo. Unioned with the adapters' own `has_filesystem`
                # flag, so this list can only ever exclude, never mis-route.
                "no_filesystem": {"skills": ["nvidia", "tokenharbor"]}},
    # How a step is classified as "changes files on disk". An explicit
    # `# requires: filesystem` / `# requires: text` in the task body always wins;
    # otherwise a task naming an existing directory counts, and failing that
    # these words decide.
    "capabilities": {
        "file_task_keywords": [
            "файл", "репозитор", "репозиторий", "коммит", "пуш", "ветк",
            "папк", "директор", "git",
            "file", "repo", "commit", "push", "branch", "folder", "directory",
            "filename",
        ],
    },
    # ── Platform-specific paths ─────────────────────────────────────────────
    # All paths are resolved relative to repo root unless absolute; "~"
    # expands. paths.state_dir is projected onto state.dir in _apply_paths,
    # so every reader keeps using state.dir. Task dirs live in [tasks].
    "paths": {
        "state_dir": ".ai_pobisk",
        "browsers_dir": ".playwright",
        "venv_dir": ".venv",
        "accounts_dir": "~/.cline/accounts",
        "model_stats_dir": "~/.ai-rotate",
        "secrets_dir": "~/.ai-rotate",
    },
    "questions": {
        "auto_answer_from": ["AGENTS.md", "README.md", "configs/", "docs/"],
        "ask_owner_when_confidence_below": 0.6,
        "max_pending": 3,
        "owner_timeout_sec": 600,
    },
    # Off by default: the integration only pays off on a small repo that
    # actually has a built graph. See configs/ai_pobisk.toml for the reasoning.
    "graft": {"enabled": False, "repo": "", "max_context_chars": 4000},
    "parallel": {"enabled": True, "max_parallel_tasks": 3,
                 "same_repo_guard": True},
    "logs": {"max_bytes": 1_000_000, "keep": 3, "journal_max_bytes": 2_000_000},
    "monitor": {"max_fatal_share": 0.3, "max_nudges_total": 6,
                "max_auth_error_models": 8, "notify_cmd": ""},
    "tasks": {"dir": "configs/tasks", "disabled": "configs/disabled",
              "skip_empty": True},
    "state": {"dir": ".ai_pobisk", "journal": "journal.md",
              "state_file": "state.json"},
    # ── mail: where confirmation letters arrive ───────────────────────────────
    # Only the coordinates live here. The password comes from
    # AI_POBISK_IMAP_PASSWORD or the 0600 shared store — never from the repo.
    "mail": {
        "host": "",
        "port": 993,
        "ssl": True,
        "user": "",
        "folder": "INBOX",
        "password_env": "AI_POBISK_IMAP_PASSWORD",
        "password_secret": "",
        "poll_sec": 5,
        "timeout_sec": 180,
        "code_regex": "",
    },
    # ── signup: an AI that fills forms on sites that have no API ──────────────
    "signup": {
        "profile": "configs/profile.toml",
        "allow_live": False,        # creating accounts is opt-in, twice
        "headless": False,          # a human should be able to watch
        "max_steps": 40,
        "max_llm_calls": 25,
        "llm_timeout_sec": 60,
        "min_confidence": 0.35,
        "mail_timeout_sec": 180,
        "stop_on_blockers": True,   # captcha / sms / payment -> ask the owner
        "accept_terms": True,       # tick the ToS box, and log that it was done
        "max_wall_clock_sec": 600,
        "candidates": [],           # empty -> reuse [meta.candidates]
        "screens_dir": ".ai_pobisk/screens",
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(repo_root):
    """Read configs/ai_pobisk.toml; fall back to DEFAULTS when missing."""
    path = os.path.join(repo_root, "configs", "ai_pobisk.toml")
    if not os.path.exists(path) or tomllib is None:
        return _apply_paths(dict(DEFAULTS)), path
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print("config: cannot parse %s (%s), using defaults" % (path, e),
              file=sys.stderr)
        return _apply_paths(dict(DEFAULTS)), path
    return _apply_paths(_merge(DEFAULTS, data)), path


def _apply_paths(cfg):
    """Single merge point for the [paths] section.

    [paths].state_dir is the Windows-friendly spelling of [state].dir; when
    it is set, it wins and is projected into state.dir, so every other reader
    keeps using state.dir and learns nothing about the split. Applied in ONE
    place instead of sprinkled over the code: callers that hand-build a CFG
    (tests) keep working, because they only ever set state.dir.
    """
    sd = (cfg.get("paths") or {}).get("state_dir")
    if sd:
        cfg.setdefault("state", {})["dir"] = sd
    return cfg

# ── Arena.ai leaderboard: live model rating for coding ──────────────────────

# Источник правды: https://arena.ai/leaderboard/code/webdev/fullstack
# (рейтинг моделей для задач fullstack-разработки). APIendpoint сайта
# защищён Cloudflare — при недоступности используется статический фолбэк
# configs/models_for_coding.toml.
ARENA_LEADERBOARD_URL = "https://arena.ai/api/v1/leaderboard/code/webdev/fullstack"
ARENA_CACHE_TTL_SEC = 24 * 3600  # keep a fetched leaderboard for one day


def _fetch_json(url, timeout_sec=15):
    """Best-effort JSON fetch. Returns parsed JSON or None on any failure."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ai_Pobisk/1.0 (https://github.com/michaelpopov/AI-Pobisk-Commander)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except Exception:
        return None


def _parse_arena_models(data):
    """Extract {model_id: score} from arena.ai leaderboard JSON.

    Tries several shapes because the exact API response is undocumented:
      * {"models": [{"id": ..., "rating": ...}, ...]}
      * {"models": [{"name": ..., "elo": ...}, ...]}
      * [{"id": ..., "score": ...}, ...]
      * [{"publicName": ..., "rank": ...}, ...]  (rank → inverted score)
    """
    models = {}

    candidates = []
    if isinstance(data, dict):
        for key in ("models", "data", "results", "items", "leaderboard"):
            val = data.get(key)
            if isinstance(val, list):
                candidates = val
                break
        if not candidates and isinstance(data.get("leaderboard"), dict):
            candidates = data["leaderboard"].get("models", [])
    elif isinstance(data, list):
        candidates = data

    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id") or entry.get("name") or entry.get("model") \
            or entry.get("publicName") or entry.get("modelId")
        if not mid:
            continue
        score = entry.get("rating", entry.get("elo", entry.get("score")))
        if score is None:
            # Convert rank (lower = better) to score: 1000 - rank
            rank = entry.get("rank", entry.get("rankByModality", {}).get("text"))
            if isinstance(rank, (int, float)) and rank > 0:
                score = 1000 - rank
            else:
                continue
        try:
            models[str(mid)] = float(score)
        except (TypeError, ValueError):
            continue
    return models


def _load_cached_arena(repo_root, cfg):
    """Return cached leaderboard dict if fresh enough, else None."""
    state_dir = (cfg.get("state") or {}).get("dir", ".ai_pobisk")
    cache_path = os.path.join(repo_root, state_dir, "arena_leaderboard_cache.json")
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        ts = cache.get("ts", 0)
        if time.time() - ts > ARENA_CACHE_TTL_SEC:
            return None
        models = cache.get("models", {})
        if models:
            return models
    except Exception:
        pass
    return None


def _save_cached_arena(repo_root, cfg, models):
    """Persist fetched leaderboard to cache."""
    state_dir = (cfg.get("state") or {}).get("dir", ".ai_pobisk")
    cache_path = os.path.join(repo_root, state_dir, "arena_leaderboard_cache.json")
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({"ts": time.time(), "models": models}, f)
    except Exception:
        pass


def load_model_ranking(repo_root, cfg):
    """Return {model_id: score} for coding tasks.

    Priority:
      1. arena.ai leaderboard (fresh cache or live fetch)
      2. configs/models_for_coding.toml (static fallback)
      3. empty dict → supervisor uses catalogue order

    Returns (models_dict, default_score).
    """
    # 1. Try live arena.ai leaderboard with cache
    cached = _load_cached_arena(repo_root, cfg)
    if cached is not None:
        return cached, 50

    live = _fetch_json(ARENA_LEADERBOARD_URL)
    if live:
        models = _parse_arena_models(live)
        if models:
            _save_cached_arena(repo_root, cfg, models)
            return models, 50

    # 2. Fall back to static TOML file
    path = os.path.join(repo_root, "configs", "models_for_coding.toml")
    if not os.path.exists(path) or tomllib is None:
        return {}, 50
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print("config: cannot parse %s (%s), ignoring model ranking"
              % (path, e), file=sys.stderr)
        return {}, 50
    return data.get("models", {}), data.get("default", {"score": 50}).get("score", 50)


def budget(cfg, key, default=0):
    return int(cfg.get("budget", {}).get(key, default))
