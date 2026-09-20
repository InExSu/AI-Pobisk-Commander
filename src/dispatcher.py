"""dispatcher.py — read configs/tasks/*.md and turn them into steps.

A task is a markdown file. Its body is the instruction; the first heading (if
any) is the title. Empty files are skipped with a note rather than treated as
failures — configs/tasks/02.безопасность.md is legitimately 0 bytes.

Step splitting is deliberately conservative: one file = one step until the
meta model learns to plan. Splitting paragraphs into steps now would create
fragile tasks that fail on formatting alone.
"""

import os


class Task:
    def __init__(self, path, title, body, order):
        self.path = path
        self.name = os.path.basename(path)
        self.title = title
        self.body = body
        self.order = order

    @property
    def empty(self):
        return not self.body.strip()

    def steps(self):
        """One step per task for now: the whole body as one instruction."""
        return [self.body.strip()] if self.body.strip() else []

    def __repr__(self):
        return "<Task %s steps=%d%s>" % (
            self.name, len(self.steps()), " EMPTY" if self.empty else "")


def _order_key(name):
    """Sort by leading number if present, else alphabetically."""
    head = name.split()[0] if name.split() else name
    digits = "".join(c for c in head if c.isdigit())
    return (0, int(digits), name) if digits else (1, 0, name)


def load_tasks(repo_root, cfg):
    """Read every .md in configs/tasks/, ordered by filename."""
    d = os.path.join(repo_root, cfg["tasks"]["dir"])
    tasks = []
    if not os.path.isdir(d):
        return tasks, "no task dir: %s" % d

    names = sorted(
        (n for n in os.listdir(d) if n.lower().endswith(".md")),
        key=_order_key)
    skipped = []
    for i, n in enumerate(names, 1):
        path = os.path.join(d, n)
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            skipped.append("%s (unreadable: %s)" % (n, e))
            continue
        title, body = _split(raw, n)
        t = Task(path, title, body, i)
        if t.empty and cfg["tasks"]["skip_empty"]:
            skipped.append("%s (empty)" % n)
            continue
        tasks.append(t)
    return tasks, skipped


def _split(raw, fallback_name):
    """First markdown heading becomes the title; the rest is the body."""
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#"):
            title = s.lstrip("#").strip()
            body = "\n".join(lines[i + 1:])
            return title or fallback_name, body
    return fallback_name, raw


def describe(tasks, skipped):
    out = []
    if tasks:
        out.append("tasks:")
        for t in tasks:
            out.append("  %-28s %s" % (t.name, t.title[:60]))
    if skipped:
        out.append("skipped:")
        for s in skipped:
            out.append("  %s" % s)
    return "\n".join(out)
