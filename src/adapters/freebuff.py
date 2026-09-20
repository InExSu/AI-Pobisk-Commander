"""FreeBuff adapter.

Freebuff is an interactive TUI with no non-interactive prompt mode, so this
adapter cannot run a task — it can only report quota state.

`freebuff-rotate.sh quota` prints the accounts x models table read-only and
exits: 0 = something usable, 5 = all accounts exhausted or blocked.

That makes freebuff a *capacity probe*, not a worker. The supervisor uses
`capacity()` to decide whether freebuff is worth dispatching to, and never
waits on it for an answer.
"""

import re

from base import Adapter, run_cmd
from outcome import Outcome, OK, ROTATE_ACCOUNT, FATAL, LOOP_GUARD


class FreeBuffAdapter(Adapter):
    skill = "freebuff"
    script = ".agents/skills/freebuff/freebuff-rotate.sh"

    def _invoke(self, prompt, model=None, account=None, timeout=None):
        # No prompt mode exists; `quota` is the only non-interactive call.
        return run_cmd([self.script_path, "quota"], timeout=timeout or 120)

    def capacity(self):
        """(usable: bool, credits: int|None, note: str)"""
        try:
            rc, out, err = self._invoke("")
        except Exception as e:
            return False, None, "quota probe failed: %s" % type(e).__name__
        text = (out or "") + "\n" + (err or "")
        if rc == 5 or "all accounts exhausted" in text.lower():
            return False, 0, "all accounts exhausted or blocked"
        if rc == 3 or not self.available():
            return False, None, "freebuff not configured"
        m = re.search(r"credits=(\d+)", text)
        credits = int(m.group(1)) if m else None
        return True, credits, "ok"

    def run(self, prompt, model=None, account=None, timeout=None):
        usable, credits, note = self.capacity()
        if not usable:
            return Outcome(ROTATE_ACCOUNT, self.skill, model or "",
                           account or "", note)
        # Usable, but there is no way to hand it a prompt non-interactively:
        # the task has to go to another skill. Report that plainly instead of
        # pretending to have tried.
        return Outcome(FATAL, self.skill, model or "", account or "",
                       "freebuff has no non-interactive mode; "
                       "launch it interactively or use another skill (%s)" % note)
