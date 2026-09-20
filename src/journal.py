"""journal.py — what ai_Pobisk did and why.

Two files under .ai_pobisk/:
  state.json  machine-readable, so an interrupted run can resume
  journal.md  human-readable, so "why did it do that" is answerable

Resume-after-kill is the point: a task that takes hours across several models
must not restart from zero because the terminal closed.
"""

import json
import os
import time


class Journal:
    def __init__(self, repo_root, cfg):
        self.repo_root = repo_root
        self.cfg = cfg
        self.dir = os.path.join(repo_root, cfg["state"]["dir"])
        self.state_path = os.path.join(self.dir, cfg["state"]["state_file"])
        self.journal_path = os.path.join(self.dir, cfg["state"]["journal"])
        os.makedirs(self.dir, exist_ok=True)
        self.state = self._load()

    # ── state ─────────────────────────────────────────────────────────────────
    def _load(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"tasks": {}, "started": time.time(), "updated": time.time()}

    def save(self):
        self.state["updated"] = time.time()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, sort_keys=True)
        os.replace(tmp, self.state_path)

    def task_state(self, name):
        return self.state["tasks"].setdefault(
            name, {"status": "pending", "step": 0, "attempts": {},
                   "notes": [], "finished": None})

    def mark(self, name, status, step=None, note=None):
        t = self.task_state(name)
        t["status"] = status
        if step is not None:
            t["step"] = step
        if note:
            t["notes"].append("%s %s" % (_ts(), note))
        if status in ("done", "failed", "skipped"):
            t["finished"] = time.time()
        self.save()

    def note(self, name, text):
        self.task_state(name)["notes"].append("%s %s" % (_ts(), text))
        self.save()

    def bump(self, name, key):
        t = self.task_state(name)
        t["attempts"][key] = t["attempts"].get(key, 0) + 1
        self.save()
        return t["attempts"][key]

    def attempts(self, name):
        return self.task_state(name)["attempts"]

    # ── journal.md ────────────────────────────────────────────────────────────
    def write_header(self, tasks, skipped, meta_status):
        # Rotate before starting a fresh run, so the journal stays readable.
        try:
            import os as _os, sys as _sys
            _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
            import logrot
            max_bytes = int((self.cfg.get("logs") or {}).get(
                "journal_max_bytes", 2_000_000))
            logrot.rotate(self.journal_path, max_bytes=max_bytes,
                          keep=int((self.cfg.get("logs") or {}).get("keep", 3)))
        except Exception:
            pass

        lines = ["# ai_Pobisk journal", "",
                 "started: %s" % _ts(),
                 "meta: %s" % (meta_status.get("model") or "rules only"),
                 "", "## tasks", ""]
        for t in tasks:
            lines.append("- %s — %s" % (t.name, t.title))
        for s in skipped:
            lines.append("- (skipped) %s" % s)
        lines.append("")
        self._append("\n".join(lines))

    def event(self, text, etype=None, **fields):
        """Write to journal.md (human) AND events.jsonl (machine).

        One call site, two sinks: monitoring cannot drift away from what the
        journal says, because there is nowhere else to log.
        """
        self._append("- %s %s" % (_ts(), text))
        try:
            import metrics
            if getattr(self, "_metrics", None) is None:
                self._metrics = metrics.Metrics(self.repo_root, self.cfg)
            fields.setdefault("detail", text[:300])
            self._metrics.emit(etype or "event", **fields)
        except Exception:
            pass

    def section(self, text):
        self._append("\n## %s\n" % text)

    def _append(self, text):
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    def reset(self):
        for p in (self.state_path, self.journal_path):
            if os.path.exists(p):
                os.remove(p)
        self.state = self._load()


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")
