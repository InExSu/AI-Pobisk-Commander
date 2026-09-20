"""OpenCode adapter.

`opencode-rotate.sh -- "prompt"` — one-shot with internal account/model
rotation. Failures surface as text (DEAD_PATTERNS: FreeUsageLimitError,
No payment method, Model is disabled, ...) plus a non-zero exit.

Unlike cline, opencode does NOT sweep every account on its own when the
prompt path fails — it picks one working pair and runs. So its failures are
worth rotating, not terminal.
"""

from base import Adapter, run_cmd


class OpenCodeAdapter(Adapter):
    skill = "opencode"
    script = ".agents/skills/opencode/opencode-rotate.sh"

    def _invoke(self, prompt, model=None, account=None, timeout=None):
        argv = [self.script_path]
        if model:
            argv += ["--model", model]
        if account:
            argv += ["--account", account]
        argv += ["--", prompt]
        return run_cmd(argv, timeout=timeout or 600)
