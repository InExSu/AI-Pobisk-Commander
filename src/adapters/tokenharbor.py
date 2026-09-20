"""Token Harbor adapter.

`tokenharbor-rotate.sh ask [-m MODEL] "prompt"` — same shape as NVIDIA:
no accounts, no TUI. Exit codes: 0 ok · 1 usage · 2 no API key ·
3 all models dead.

Only 4 free models, so rotation depth is thin — the supervisor should treat
"all models dead" here as a reason to switch skill rather than to retry hard.
"""

from base import Adapter, run_cmd
from outcome import Outcome, OK, FATAL, ROTATE_MODEL


class TokenHarborAdapter(Adapter):
    skill = "tokenharbor"
    script = ".agents/skills/tokenharbor/tokenharbor-rotate.sh"

    def _invoke(self, prompt, model=None, account=None, timeout=None):
        argv = [self.script_path, "ask"]
        if model:
            argv += ["-m", model]
        argv.append(prompt)
        return run_cmd(argv, timeout=timeout or 300)

    def run(self, prompt, model=None, account=None, timeout=None):
        import time
        from classify import classify
        t0 = time.time()
        try:
            rc, out, err = self._invoke(prompt, model, account, timeout)
        except Exception as e:
            return Outcome(FATAL, self.skill, model or "", account or "",
                           "adapter error: %s" % type(e).__name__)
        ms = int((time.time() - t0) * 1000)
        text = (out or "") + "\n" + (err or "")

        if rc == 0:
            return Outcome(OK, self.skill, model or "", account or "",
                           "done", None, ms, text)
        if rc == 2:
            return Outcome(FATAL, self.skill, model or "", account or "",
                           "no Token Harbor API key in the secret store",
                           None, ms, text)
        if rc == 3:
            return Outcome(ROTATE_MODEL, self.skill, model or "", account or "",
                           "all Token Harbor models dead", None, ms, text)
        return classify(text, skill=self.skill, model=model or "",
                        account=account or "", elapsed_ms=ms)

    def models(self):
        try:
            rc, out, _ = run_cmd([self.script_path, "models"], timeout=60)
            return [l.strip() for l in out.splitlines() if l.strip()]
        except Exception:
            return []
