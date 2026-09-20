"""config.py — load configs/ai_pobisk.toml, with sane defaults if absent.

Zero dependencies: `tomllib` is stdlib since 3.11.
"""

import os
import sys

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
    },
    "workers": {"order": ["nvidia", "tokenharbor", "opencode", "cline"],
                "non_workers": ["freebuff"]},
    "questions": {
        "auto_answer_from": ["AGENTS.md", "README.md", "configs/", "docs/"],
        "ask_owner_when_confidence_below": 0.6,
        "max_pending": 3,
        "owner_timeout_sec": 600,
    },
    "graft": {"enabled": True, "repo": "", "max_context_chars": 4000},
    "parallel": {"enabled": True, "max_parallel_tasks": 3,
                 "same_repo_guard": True},
    "logs": {"max_bytes": 1_000_000, "keep": 3, "journal_max_bytes": 2_000_000},
    "monitor": {"max_fatal_share": 0.3, "max_nudges_total": 6,
                "max_auth_error_models": 8, "notify_cmd": ""},
    "tasks": {"dir": "configs/tasks", "skip_empty": True},
    "state": {"dir": ".ai_pobisk", "journal": "journal.md",
              "state_file": "state.json"},
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
        return dict(DEFAULTS), path
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print("config: cannot parse %s (%s), using defaults" % (path, e),
              file=sys.stderr)
        return dict(DEFAULTS), path
    return _merge(DEFAULTS, data), path


def budget(cfg, key, default=0):
    return int(cfg.get("budget", {}).get(key, default))
