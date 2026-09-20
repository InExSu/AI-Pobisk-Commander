"""graft.py — give the worker model a map of the codebase, not a blind start.

Why: a coding agent spends most of its tool calls, tokens and latency
re-exploring a repo it already mapped last session and threw away. Graft
(trailhq/Graft) builds that map once and serves it as `graft ask` /
`graft map` / `graft skeleton`. Their measured effect: ~42% fewer tokens,
~46% fewer tool calls, ~60% less time, with equal-or-better correctness.

Integration shape: ai_Pobisk prepends a short graph context to the worker's
prompt. Nothing else changes — the worker still uses its own tools.
`graft build` and every read command are deterministic tree-sitter: no model,
no key, no network, sub-second on this repo.

Failure policy: if graft is missing or the graph is stale/absent, this module
returns "" and the run proceeds exactly as before. Context is an accelerator,
never a dependency.
"""

import json
import os
import subprocess
import sys

# Budget: the context must stay a small prefix, or it costs more than it saves.
MAX_CONTEXT_CHARS = 4000
TIMEOUT = 60


def _run(args, cwd, timeout=TIMEOUT):
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", "graft not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "graft timed out"
    except Exception as e:
        return 1, "", "%s: %s" % (type(e).__name__, e)


def available():
    """Is the graft CLI usable?"""
    rc, out, _ = _run(["graft", "--version"], cwd=None, timeout=20)
    return rc == 0


def has_graph(repo_root):
    """Is there a built graph in this repo?"""
    return os.path.isdir(os.path.join(repo_root, "graft"))


def build(repo_root, deep=False, extensions=None, timeout=300):
    """Build/refresh the graph. Structural pass needs no key.

    `deep` adds LLM-written summaries and DOES need a provider key — it is
    opt-in and never run automatically.
    """
    args = ["graft", "build", "."]
    if deep:
        args.append("--deep")
    if extensions:
        args += ["--extensions"] + list(extensions)
    return _run(args, cwd=repo_root, timeout=timeout)


def ask(repo_root, query, limit=8):
    """Ranked nodes for a question. Returns a short text block, or ""."""
    if not query:
        return ""
    rc, out, _ = _run(["graft", "ask", query, "--json"], cwd=repo_root)
    if rc != 0:
        return ""
    try:
        d = json.loads(out)
    except Exception:
        return ""
    hits = d.get("hits") or []
    if not hits:
        return ""
    lines = []
    for h in hits[:limit]:
        # graft ask --json emits {title, pointer, snippet, score}. Older/newer
        # builds may use name/path, so read both shapes.
        title = (h.get("title") or h.get("name")
                 or h.get("symbol") or "").strip()
        where = (h.get("pointer") or h.get("path") or h.get("file")
                 or "").strip()
        if not title and not where:
            continue
        line = "- %s" % (title or where)
        if where and title and where not in title:
            line += " — %s" % where
        snippet = (h.get("snippet") or "").strip()
        if snippet:
            # One line only: the snippet is a signature or a crux, not a body.
            line += "\n    %s" % snippet.splitlines()[0][:120]
        lines.append(line)
    if not lines:
        return ""
    return "Relevant code (from the code graph):\n" + "\n".join(lines)


def repo_map(repo_root):
    """Orientation: directory clusters, hubs, hotspots. Short text, or ""."""
    if not has_graph(repo_root):
        return ""
    rc, out, _ = _run(["graft", "map"], cwd=repo_root)
    if rc != 0 or not out:
        return ""
    # Strip graft's own "tokens saved" advertising line.
    keep = [l for l in out.splitlines()
            if not l.startswith("[graft]") and "tokens saved" not in l]
    return "\n".join(keep).strip()


def context_for(repo_root, prompt, cfg=None):
    """The block ai_Pobisk prepends to a worker prompt.

    Two parts: a ranked answer to the specific question, plus a repo map for
    orientation. Both are truncated to MAX_CONTEXT_CHARS — a context prefix
    that is too big costs more than the exploration it removes.
    """
    if not available() or not has_graph(repo_root):
        return ""
    parts = []
    a = ask(repo_root, prompt[:300])
    if a:
        parts.append(a)
    m = repo_map(repo_root)
    if m:
        parts.append("Repository map:\n" + m)
    if not parts:
        return ""
    block = "\n\n".join(parts)
    if len(block) > MAX_CONTEXT_CHARS:
        block = block[:MAX_CONTEXT_CHARS].rsplit("\n", 1)[0] + "\n…"
    return block


def status(repo_root):
    return {"installed": available(), "graph": has_graph(repo_root)}
