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
    "tasks": {"dir": "configs/tasks", "skip_empty": True},
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
