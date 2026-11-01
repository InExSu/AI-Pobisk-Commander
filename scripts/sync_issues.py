#!/usr/bin/env python3
"""sync_issues.py — turn GitHub issues into Pobisk task files.

Pobisk works on *any* project whose issues live on GitHub. This script is the
missing bridge: given a repo and (optionally) a label, it pulls the open
issues with `gh` and writes one task file per issue under configs/tasks/.

The generated task file is self-contained:

    # GitHub issue #777: <title>
    # repo: owner/name  url: https://github.com/...
    /abs/path/to/local/clone/
    # requires: filesystem

    <full issue body>

    ---
    Инструкция воркеру (Pobisk): ...

The project directory on its own line is what lets Pobisk (a) recognise the
task as a *file step* (so it routes to a filesystem worker — cline/opencode —
and skips the no-FS completion APIs), and (b) actually run the worker INSIDE
that repo: the supervisor exports PB_TARGET_DIR from this path, and the worker
subprocess is launched there. The agent then edits the project, not Pobisk's
own tree.

Idempotent: an issue that already has a task file is skipped, so re-running
`issues` never clobbers work in progress.

Usage:
    python3 scripts/sync_issues.py --repo InExSu/igrasu.ru \
        --dir /Users/.../_Games/igrasu.ru [--label agent-ready] [--issue 777]

Dependencies: the GitHub CLI (`gh`), authenticated (`gh auth status`).
"""
import argparse
import json
import os
import re
import subprocess
import sys

# Directives the worker always gets, generic to any repo. Repo-specific rules
# (AGENTS.md / CLAUDE.md / .clinerules / CONTRIBUTING.md) are read by the agent
# itself from the project directory — Pobisk only points it at them.
_WORKER_PREAMBLE = """\
---
Инструкция воркеру (Pobisk):
- Работай в этом каталоге: {dir} (он указан выше и продублирован в промпте как
  «Work in this directory»). Запускай инструменты оттуда — правь проект, а не
  дерево ai_Pobisk.
- Перед правками прочти КОНВЕНЦИИ репозитория, если они есть, и следуй им:
  AGENTS.md, CLAUDE.md, .clinerules, .agnes, CONTRIBUTING.md, README.md.
  Не выдумывай правила — бери их из этих файлов.
- Git-дисциплина: коммить СТРОГО по pathspec — `git commit -o -m "…" -- <файлы>`;
  НЕ используй `git add -A` и любые «добавить всё». Новые файлы сначала
  `git add <файл>`, потом коммить по pathspec.
- Сделай ветку под задачу (например feat/issue-{num}) и пуши ТОЛЬКО в неё.
  НЕ мержи в main и НЕ деплой, если явно не сказано.
- Отвечай на языке проекта (для igrasu.ru — по-русски).
- Закончил — оставь результат в журналах репозитория, если они предусмотрены.
"""


def _gh(args):
    """Run a `gh` subcommand, return parsed JSON. Raises on failure."""
    cmd = ["gh"] + args
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError("gh not found on PATH — install the GitHub CLI")
    if p.returncode != 0:
        raise RuntimeError("gh failed (%s): %s" % (
            " ".join(cmd), (p.stderr or p.stdout).strip()[:300]))
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError("gh output was not JSON: %s" % e)


def _require_gh_auth(repo):
    """Fail early with a clear message if gh is missing or not logged in."""
    try:
        _gh(["auth", "status"])
    except RuntimeError as e:
        raise RuntimeError(
            "GitHub CLI not ready: %s\n"
            "Install `gh` and run `gh auth login` (or `gh auth status`)."
            % e)
    # Confirm the repo is reachable.
    _gh(["repo", "view", repo, "--json", "name"])


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(title, limit=42):
    """ASCII slug for a filename — lowercase, dashes, no collisions handled by
    the caller."""
    s = (title or "").lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    if len(s) > limit:
        s = s[:limit].rstrip("-")
    return s or "issue"


def fetch_issues(repo, label=None, state="open", limit=10, issue=None):
    """Return a list of issue dicts: number, title, url, body."""
    if issue:
        d = _gh(["issue", "view", str(issue), "--repo", repo,
                 "--json", "number,title,url,body"])
        items = [d]
    else:
        q = ["issue", "list", "--repo", repo, "--state", state,
             "--limit", str(limit), "--json", "number,title,url,body,labels"]
        if label:
            q += ["--label", label]
        items = _gh(q)
    # `gh issue list` sometimes returns a short/None body; backfill from view.
    out = []
    for it in items:
        body = it.get("body")
        if not body:
            try:
                body = _gh(["issue", "view", str(it["number"]), "--repo", repo,
                            "--json", "body"]).get("body") or ""
            except RuntimeError:
                body = ""
        out.append({"number": it["number"], "title": it.get("title", ""),
                    "url": it.get("url", ""), "body": body or ""})
    return out


def render_task(repo, issue, project_dir=""):
    """Build the task-file markdown for one issue."""
    head = "# GitHub issue #%s: %s\n" % (issue["number"],
                                         (issue["title"] or "").strip())
    head += "# repo: %s  url: %s\n" % (repo, issue.get("url", ""))
    body = (issue.get("body") or "").strip()
    # The project directory is its own line so Pobisk's _target_dir_from_task
    # captures it as the FIRST absolute path (before any path inside the body).
    dir_line = ("%s/\n" % project_dir.rstrip("/")) if project_dir else ""
    fs_line = "# requires: filesystem\n" if project_dir else ""
    preamble = _WORKER_PREAMBLE.format(dir=project_dir) if project_dir else \
        _WORKER_PREAMBLE.format(dir="<project directory>")
    parts = [head, dir_line, fs_line, "\n", body, "\n", preamble]
    return "".join(parts)


def sync(repo, project_dir="", label=None, state="open", limit=10,
         issue=None, out_dir="configs/tasks", dry_run=False):
    """Pull issues and write task files. Returns (created, skipped)."""
    _require_gh_auth(repo)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    issues = fetch_issues(repo, label=label, state=state, limit=limit,
                          issue=issue)
    created = skipped = 0
    for it in issues:
        name = "%s-%s.md" % (it["number"], slugify(it["title"]))
        path = os.path.join(out_dir, name)
        if os.path.exists(path):
            skipped += 1
            print("  skip  %s (exists)" % name)
            continue
        text = render_task(repo, it, project_dir=project_dir)
        if dry_run:
            print("  would write %s (%d bytes)" % (name, len(text)))
            created += 1
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        created += 1
        print("  wrote %s" % name)
    return created, skipped


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="sync_issues.py", description="pull GitHub issues into task files")
    p.add_argument("--repo", required=True,
                   help="owner/name (e.g. InExSu/igrasu.ru) or full URL")
    p.add_argument("--dir", default="",
                   help="local checkout of the repo; sets Work-in-directory")
    p.add_argument("--label", default="", help="only issues with this label")
    p.add_argument("--state", default="open")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--issue", type=int, default=0,
                   help="pull a single issue number instead of a list")
    p.add_argument("--out", default="configs/tasks",
                   help="output directory for task files")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    if a.dir and not os.path.isdir(a.dir):
        print("warning: --dir %s is not a directory; task will be a text step"
              % a.dir, file=sys.stderr)

    _require_gh_auth(a.repo)
    repo = _normalise_repo(a.repo)
    created, skipped = sync(
        repo, project_dir=a.dir, label=a.label or None, state=a.state,
        limit=a.limit, issue=a.issue or None, out_dir=a.out,
        dry_run=a.dry_run)
    print("\nissues: %d created, %d skipped" % (created, skipped))
    return 0


def _normalise_repo(repo):
    """Accept owner/name or a full github URL; return owner/name."""
    repo = (repo or "").strip()
    m = re.search(r"github\.com/([^/\s]+/[^/\s]+?)(?:\.git)?/?$", repo)
    if m:
        return m.group(1)
    return repo


if __name__ == "__main__":
    sys.exit(main())
