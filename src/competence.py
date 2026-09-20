"""competence.py — is this question ai_Pobisk's to answer?

When a worker model asks something ("which database should I use?"),
ai_Pobisk decides:

  1. Answerable from its own sources (AGENTS.md, README.md, configs/, docs/)
     -> answer itself, no owner involved.
  2. Not answerable -> escalate to the owner, log the pending question.
  3. Ambiguous -> ask the meta model to judge.

The sources are read fresh every time, so editing AGENTS.md changes what
ai_Pobisk considers itself competent about — no reconfiguration needed.
"""

import os
import re

MAX_SOURCE_BYTES = 400_000      # don't read the whole repo into a prompt
MAX_SNIPPET = 1200


def _sources(repo_root, cfg):
    """Load the files listed in questions.auto_answer_from."""
    out = []
    for rel in cfg["questions"]["auto_answer_from"]:
        p = os.path.join(repo_root, rel)
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs
                           if d not in (".git", "node_modules", "__pycache__")]
                for fn in sorted(files):
                    if fn.endswith((".md", ".toml", ".txt")):
                        out.append(os.path.join(root, fn))
        elif os.path.isfile(p):
            out.append(p)
    return out


def _load(path):
    try:
        if os.path.getsize(path) > MAX_SOURCE_BYTES:
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _keyword_hits(question, text):
    """Cheap lexical overlap: how much of the question appears in the source."""
    words = set(re.findall(r"[A-Za-zА-Яа-яЁё0-9_]{4,}", question.lower()))
    if not words:
        return 0.0
    low = text.lower()
    hit = sum(1 for w in words if w in low)
    return hit / len(words)


def find_context(repo_root, cfg, question):
    """Return (best_source_path, snippet, score) from local sources."""
    best = (None, "", 0.0)
    for path in _sources(repo_root, cfg):
        text = _load(path)
        if not text:
            continue
        score = _keyword_hits(question, text)
        if score > best[2]:
            # Centre the snippet on the first hit, so it is relevant.
            idx = 0
            words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9_]{4,}",
                                           question.lower())
                     if w in text.lower()]
            if words:
                idx = max(0, text.lower().find(words[0]) - 200)
            best = (path, text[idx:idx + MAX_SNIPPET], score)
    return best


def answer_question(repo_root, cfg, meta, question, journal, task_name):
    """Decide and answer, or return None to escalate to the owner.

    Returns the answer string (which gets appended to the step prompt), or
    None when the owner must be asked.
    """
    if not question:
        return None

    floor = float(cfg["questions"]["ask_owner_when_confidence_below"])
    max_pending = int(cfg["questions"]["max_pending"])

    path, snippet, score = find_context(repo_root, cfg, question)
    journal.event("question: %r (local score %.2f from %s)"
                  % (question[:80], score, path or "nothing"))

    # 1. Confident it is in our own docs: answer from them.
    if score >= max(0.5, floor) and snippet:
        return ("This is decided by the project itself (%s):\n%s"
                % (os.path.relpath(path, repo_root), snippet))

    # 2. Ask the meta model, giving it the local context.
    if meta and meta.model:
        prompt = (
            "A worker model asked ai_Pobisk a question. Decide whether "
            "ai_Pobisk can answer it from its own project sources, or whether "
            "the human owner must be asked.\n\n"
            "Question: %s\n\n"
            "Local sources say (%s):\n%s\n\n"
            "Answer JSON: {\"can_answer\": true|false, \"confidence\": 0-1, "
            "\"answer\": \"your answer if can_answer\"}"
            % (question[:600], path or "nothing found", snippet[:1000]))
        try:
            from outcome import Outcome, QUESTION
            o = meta.adapters[meta.model[0]].run(
                prompt, model=meta.model[1],
                timeout=cfg["meta"]["timeout_sec"])
            d = _parse_bool((o.raw or ""))
            if d and d.get("can_answer") and d.get("answer"):
                conf = float(d.get("confidence", 0.5))
                if conf >= floor:
                    journal.event("meta answered question (conf %.2f)" % conf)
                    return d["answer"]
        except Exception:
            pass

    # 3. Escalate. Cap the number of pending questions so we never spam.
    pending = journal.task_state(task_name).get("pending_questions", [])
    if len(pending) >= max_pending:
        journal.event("too many pending questions (%d) — stopping" % len(pending))
        return None
    pending.append({"question": question[:400], "at": _now()})
    journal.task_state(task_name)["pending_questions"] = pending
    journal.save()
    journal.event("ESCALATED to owner: %s" % question[:100])
    return None


def _parse_bool(text):
    if not text:
        return None
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return None
    import json
    try:
        return json.loads(text[s:e + 1])
    except Exception:
        return None


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
