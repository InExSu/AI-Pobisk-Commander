"""Kilo adapter.

`kilo run -m <provider>/<model> "<prompt>"` — one-shot chat completion via
the Kilo CLI (https://github.com/kilocode/kilo). No accounts, no TUI, pure
HTTP through the Kilo gateway. Exit 0 = ok, non-zero = failure.

Free models are those with `:free` suffix (or zero cost in the models.dev
catalogue). The free-model catalogue mirrors what `cline_Models_Free_Make.sh`
extracts from the cline binary — same `kilo` router catalogue.
"""

import shutil

from base import Adapter, run_cmd
from outcome import Outcome, FATAL, ROTATE_MODEL


class KiloAdapter(Adapter):
    skill = "kilo"
    script = "kilo"   # resolved from PATH: /opt/homebrew/bin/kilo on macOS
    # Kilo CLI does not write the shared health store; the adapter does.
    script_records_health = False
    # Bare chat completion: no filesystem, no tools.
    has_filesystem = False

    def available(self):
        """Kilo is usable when the `kilo` binary is on PATH."""
        return bool(shutil.which("kilo"))

    def _invoke(self, prompt, model=None, account=None, timeout=None):
        argv = ["kilo", "run"]
        if model:
            argv += ["-m", model]
        argv += ["--auto", "--format", "json"]
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
            # rc 0 is not proof of work: a bare completion asked to edit files
            # answers "I don't have access to your filesystem" in polite prose
            # and exits 0. classify() knows that shape and reports
            # rotate_account; for a normal answer it returns ok.
            return classify(text, skill=self.skill, model=model or "",
                            account=account or "", elapsed_ms=ms, rc=rc)
        if rc == 2:
            return Outcome(FATAL, self.skill, model or "", account or "",
                           "no Kilo API key / credentials", None, ms, text)
        if rc == 3:
            return Outcome(ROTATE_MODEL, self.skill, model or "", account or "",
                           "all Kilo models dead", None, ms, text)
        return classify(text, skill=self.skill, model=model or "",
                        account=account or "", elapsed_ms=ms, rc=rc)

    def models(self):
        """Free models from the Kilo router catalogue (`:free` suffix).

        Mirror of `cline-models-free.txt` (same kilo router catalogue),
        prefixed with `kilo/` as required by the `kilo run -m` flag.
        """
        return [
            "kilo/cline-free/deepseek-v4.1-flash",
            "kilo/cline-free/muse-spark-1.3-contributor",
            "kilo/z-ai/glm-5.3-flash",
            "kilo/cline-free/solar-pro4",
            "kilo/poolside/laguna-s-2.1:free",
            "kilo/inclusionai/ling-3.0-flash-vl:free",
            "kilo/nex-agi/nex-n2.5-mini:free",
            "kilo/nex-agi/nex-n2.5-pro:free",
            "kilo/inclusionai/ling-3.0-flash-sante:free",
            "kilo/inclusionai/ling-3.0-flash-fin:free",
            "kilo/dots-studio/dots-3-note-preview:free",
            "kilo/liquid/lfm-2.5-2.6b:free",
            "kilo/nvidia/nemotron-3.5-lightning:free",
            "kilo/thinkingmachines/inkling-small:free",
            "kilo/poolside/laguna-xs-2.1:free",
            "kilo/cohere/north-mini-code:free",
            "kilo/nvidia/nemotron-3-ultra-550b-a55b:free",
            "kilo/stepfun/step-3.7-flash:free",
            "kilo/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "kilo/nvidia/nemotron-3-super-120b-a12b:free",
            "kilo/openrouter/free",
        ]
