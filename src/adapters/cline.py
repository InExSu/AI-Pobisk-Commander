"""cline adapter.

`cline-rotate.sh "prompt"` — act mode, auto-approve. Rotates models and
accounts itself; exit codes: 0 ok · 1 task failed · 3 no accounts ·
4 loop guard · 5 all accounts exhausted.

Because cline already rotates internally, its exit codes are mostly
*terminal*: by the time it returns 5, it has exhausted everything it knows.
The adapter therefore maps 4/5 to loop_guard/fatal rather than asking the
supervisor to rotate — there is nothing left to rotate to.
"""

import time

from base import Adapter, run_cmd
from outcome import Outcome, OK, FATAL, LOOP_GUARD, ROTATE_ACCOUNT


class ClineAdapter(Adapter):
    skill = "cline"
    script = ".agents/skills/cline/cline-rotate.sh"

    def _invoke(self, prompt, model=None, account=None, timeout=None):
        argv = [self.script_path]
        if account:
            # cline picks accounts from ~/.cline/accounts; pinning is env-driven
            pass
        argv.append(prompt)
        return run_cmd(argv, timeout=timeout or 900)

    def run(self, prompt, model=None, account=None, timeout=None):
        t0 = time.time()
        try:
            rc, out, err = self._invoke(prompt, model, account, timeout)
        except Exception as e:
            return Outcome(FATAL, self.skill, model or "", account or "",
                            "adapter error: %s" % e)
        ms = int((time.time() - t0) * 1000)
        text = (out or "") + "\n" + (err or "")

        # cline's own exit codes are authoritative: it has already rotated
        # through every model and account it could before returning these.
        if rc == 0:
            return Outcome(OK, self.skill, model or "", account or "",
                           "done", None, ms, text)
        if rc == 3:
            return Outcome(FATAL, self.skill, model or "", account or "",
                           "no cline accounts configured", None, ms, text)
        if rc == 4:
            return Outcome(LOOP_GUARD, self.skill, model or "", account or "",
                           "cline loop guard tripped", None, ms, text)
        if rc == 5:
            return Outcome(ROTATE_ACCOUNT, self.skill, model or "",
                           account or "", "cline: all accounts exhausted",
                           None, ms, text)
        # rc 1 = task failed; let classify decide whether it is retryable
        from classify import classify
        return classify(text, skill=self.skill, model=model or "",
                        account=account or "", elapsed_ms=ms)
