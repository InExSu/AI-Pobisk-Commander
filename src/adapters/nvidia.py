"""NVIDIA NIM adapter.

`nvidia-rotate.sh ask [-m MODEL] "prompt"` — no accounts, no TUI, pure HTTP.
Exit codes: 0 ok · 1 usage · 2 no API key · 3 all models dead.

This is the most reliable worker: no OAuth, no TUI, no account rotation.
Exit 2 (missing key) is fatal for the skill but not for the task — the
supervisor should fall through to another skill.
"""

from base import Adapter, run_cmd
from outcome import Outcome, OK, FATAL, ROTATE_MODEL


class NvidiaAdapter(Adapter):
    skill = "nvidia"
    script = ".agents/skills/nvidia/nvidia-rotate.sh"

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
                           "no NVIDIA API key in the secret store", None, ms, text)
        if rc == 3:
            return Outcome(ROTATE_MODEL, self.skill, model or "", account or "",
                           "all NVIDIA models dead", None, ms, text)
        return classify(text, skill=self.skill, model=model or "",
                        account=account or "", elapsed_ms=ms)

    def models(self):
        try:
            rc, out, _ = run_cmd([self.script_path, "models"], timeout=60)
            return [l.strip() for l in out.splitlines() if l.strip()]
        except Exception:
            return []
