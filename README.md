# AI-Pobisk-Commander

[🇷🇺 ReadMe_RU.md — русская версия](ReadMe_RU.md)

Control and Management of AI Agents based on the systems-engineering approach of Pobisk Kuznetsov.

`ai_Pobisk` is a console supervisor that launches CLI AI agents (Cline, OpenCode, NVIDIA NIM, Token Harbor, FreeBuff, etc.), manages their rotation, health, budgets, and evidence — so tasks complete autonomously without freezing on a dead model, empty quota, or a confirmation prompt.

---

## Quick Start

```bash
# clone
git clone <repo>
cd AI-Pobisk-Commander

# create venv + install deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright .venv/bin/python -m playwright install chromium

# run all tasks from configs/tasks/
./ai_Pobisk.sh

# run a single task
./ai_Pobisk.sh --task "03 добавка.md"

# dry run — print plan, call no model
./ai_Pobisk.sh --dry-run

# list discovered tasks
./ai_Pobisk.sh list

# agents and availability
./ai_Pobisk.sh skills

# self-test: meta-model + adapters + state machine + tasks
./ai_Pobisk.sh self-test
```

---

## What ai_Pobisk Does

`ai_Pobisk` takes a task file from `configs/tasks/*.md`, hires a worker model through `.agents/skills/`, and supervises it:

- **Model stopped responding** — ping it until it is clear whether it is alive or dead
- **Model quota exhausted** — switch to another model
- **Account quota exhausted on all models** — hand the task to another agent
- **No infinite loops** — bounded by step, iteration, retry, and nudge budgets
- **Model asked a question** — decide whether to answer or ask the owner

---

## CLI Commands

| Command | Purpose |
|---|---|
| `./ai_Pobisk.sh` | run all tasks from `configs/tasks/` |
| `./ai_Pobisk.sh --task NAME` | run only this task |
| `./ai_Pobisk.sh --dry-run` | print plan, call no model |
| `./ai_Pobisk.sh list` | discovered tasks |
| `./ai_Pobisk.sh skills` | agents and availability |
| `./ai_Pobisk.sh skills --probe` | actually ping each model (spends quota) |
| `./ai_Pobisk.sh self-test` | meta-model + adapters + machine + tasks |
| `./ai_Pobisk.sh status` | task state and questions to owner |
| `./ai_Pobisk.sh status --live` | aggregate current run |
| `./ai_Pobisk.sh watch` | live view during a run |
| `./ai_Pobisk.sh tail --follow` | last events, like `tail -f` |
| `./ai_Pobisk.sh check` | thresholds; exit 1 if breached |
| `./ai_Pobisk.sh serve` | `/metrics` in Prometheus format |
| `./ai_Pobisk.sh report` | post-hoc breakdown of last run |
| `./ai_Pobisk.sh drakon` | state machine and DRAKON silhouette |
| `./ai_Pobisk.sh drakon check` | verify machine is sound |
| `./ai_Pobisk.sh mail --wait 180` | wait for a confirmation email |
| `./ai_Pobisk.sh signup URL --yes` | AI fills a registration form |

---

## How the Supervisor Works

The supervisor takes a task from `configs/tasks/*.md`, hires a worker model through `.agents/skills/`, and watches it. If the model stops, it determines whether the failure is fatal and continues if not. If the model asks a question, it decides whether the question is within its competence.

Two rules learned from live runs:

1. **Everything above the first `#` heading stays in the task body.** The heading is a log label, not a boundary of the instruction. Earlier the preamble was silently dropped, and a task whose first line was a repository path lost that path: the worker agent ran in `ai_Pobisk` instead of the target repository.
2. **Editing a task is a new task.** `state.json` stores the body hash; change the text — step resets to 0 and the task runs again. Without this, an edited instruction was considered `done` and the run finished in one second, doing nothing. To force re-running everything — use `--fresh`.

### Resuming After a Crash and `--fresh`

By default the run **resumes**. `.ai_pobisk/state.json` marks each completed step; a restart after a crash skips what was done and starts from the next step. On start, the supervisor also checks `status.json` for a frozen `running` state and removes `worker_heartbeat.json` left by a dead run — so an outside observer does not mistake a dead run for a live one (`_recover_if_crashed`).

`--fresh` **wipes** `state.json` and `journal.md` and forces all tasks to start from step 0. Use it to re-run everything forcibly: after editing an instruction, if re-hashing did not help, or simply to re-run regardless of history.

`--fresh` **does not** touch `.ai-rotate/model-stats.json` (model health) — this is a shared store across skills, it survives runs, and resetting it at every restart would lose the rotation's picture of "who is alive". Reset health separately:

```bash
python3 .agents/skills/_shared/model-stats.py reset <skill>
```

On start it configures its own **meta-model** (fast and cheap, separate from workers). If the meta-model is unavailable — it degrades to the rules table, it does not freeze.

```
task -> skill adapter -> Outcome (7 outcomes)
  ok/retry/rotate_model  -> continue
  rotate_account         -> another account/skill
  question               -> competence: answer yourself or ask the owner
  fatal/loop_guard       -> stop, report
```

Key decision: **a single outcome dictionary** (`_shared/outcome.py`). Five skills reported errors in five dialects — cline banner text, opencode codes 0/1/2, freebuff 1/4/5, nvidia/th exit 3 + HTTP. Adapters in `src/adapters/` translate everything into seven outcomes; **the skills themselves were not changed**.

One rule learned from hours wasted: **a killed worker is the model's fault, not the provider's.** When a worker had to be killed (silence guard, `rc < 0`) or killed by step timeout (`rc 124`), retrying with the same model repeats the hang — for the full `step_timeout_sec`. So a killed worker gives `rotate_model` immediately; `retry` remains for short provider failures (5xx, 429) and unrecognized exit codes.

Parallel execution also learned from one run: three tasks on one repository went parallel due to a trailing slash in the path (`repo/` ≠ `repo` as strings), and two `state.json` saves from different threads killed each other on a shared tmp file. Now paths are normalized (`same_repo_guard` compares one repository as one string), tmp names are unique per thread, and the worker's target directory lives in thread-local, not in the shared `os.environ`.

### State Machine and DRAKON Silhouette

Control is not an `if` ladder in the supervisor, but a transition table (`src/statemachine.py`): 13 states, 27 transitions, 5 terminals. The supervisor only executes what the machine decides — call adapter, counter, rotation, question to meta. Therefore the picture and the code do not diverge.

```bash
./ai_Pobisk.sh drakon                    # text silhouette
./ai_Pobisk.sh drakon --svg docs/machine.svg
./ai_Pobisk.sh drakon check              # machine is connected, no dead ends
```

Ready picture is in [`docs/machine.svg`](docs/machine.svg); rebuilt by the same command, so it cannot go stale — drift is caught by `drakon check`.

`drakon check` proves three properties: every state is reachable from `dispatch`; every non-terminal state has an exit; from every state a terminal can be reached. Plus a fourth property about the picture itself: **every branch of the silhouette exists in the transition table** — the test does not allow drawing a non-existent path.

Silhouette trunk — task step, right branches — recovery paths. Branch order is the decision priority: first invariants (budgets, keys, "no filesystem", confirmations, `loop_guard`), then cases the code closes itself (nudges, questions), and only then the meta-model.

State and action breakdown — `.ai_pobisk/{state.json,journal.md}`. Killed the process mid-run — restart continues from the same step.

### Monitoring

```bash
./ai_Pobisk.sh status              # task state
./ai_Pobisk.sh status --json       # same for scripts
./ai_Pobisk.sh status --live       # aggregates of current run
./ai_Pobisk.sh status --live --json
./ai_Pobisk.sh watch               # live view during a run
./ai_Pobisk.sh watch --json        # one JSON line per tick, for jq/bot
./ai_Pobisk.sh tail --follow       # last events, like tail -f
./ai_Pobisk.sh check               # thresholds; exit 1 if something wrong
./ai_Pobisk.sh check --notify-cmd 'curl -s ...'   # command on trigger
./ai_Pobisk.sh serve               # /metrics in Prometheus format
./ai_Pobisk.sh serve --port 0      # free port, number is printed
./ai_Pobisk.sh report              # breakdown of past run
./ai_Pobisk.sh report --run ID     # breakdown of specific run
./ai_Pobisk.sh skills --probe      # actually ping models (spends requests)
```

Every event is written both to `journal.md` (human) and `events.jsonl` (machine) — from one point, so they do not diverge. Aggregates: duration, p50/p95 step, error share, ok% per skill, number of nudges and rotations.

Thresholds in `check`: fatal share, nudge count, p95 vs median, approaching step timeout. All on stdlib — Prometheus is not needed.

### Who Decides: Model or Code

**The resolver is the meta-model.** Code describes only invariants:

| Invariant (code, model does not vote) | Why |
|---|---|
| budgets and iteration limit | otherwise "retry" forever |
| credentials and empty wallet | retry cannot fix it |
| "no filesystem" for bare APIs | missing capability, not bad luck |
| confirmations (`Press Enter`) | nobody at the keyboard; answer always "continue" |
| `loop_guard` | already looping |

Everything else — model behavior diversity — goes to the meta-model. It answers with one of the agreed outcomes; code checks the answer and enforces budgets. If the meta-model is unavailable, the rules table works: degradation, not stop.

Invariants live in one place — `statemachine.invariant()`; the check order is there too. To change recovery behavior, change the transition table, not the supervisor: the supervisor only executes the chosen state.

### Code Context (Graft) — Off by Default

Graft lays out a repository into a folder of linked markdown cards, and ai_Pobisk can prepend a short map to the worker prompt so it starts oriented instead of exploring from scratch. Claimed Graft: **−42% tokens, −46% tool calls**. The number is from their repository, not from here.

**Off by default, and by measurement, not taste:**

1. Tasks hit `igrasu.ru`: 19 GB, 53,505 files. A graph there is not a prompt prefix, it is a separate project; and `graft/` there is not in `.gitignore`, so litter would get into someone else's repository.
2. `graft ask` indexes identifiers and English prose. A Russian question ("how does the supervisor decide to change model") gets **zero hits**, the same question in English — eight with exact `file:line`. All task files here are Russian, so the expensive half of the integration is guaranteed empty.
3. It is the only non-stdlib dependency in the project. "Cloned and works" matters more than "maybe −42%" on a repository with no graph.

```bash
./ai_Pobisk.sh graft              # status: CLI, graph, enabled in prompts
./ai_Pobisk.sh graft build        # build graph (tree-sitter, no key)
./ai_Pobisk.sh graft ask "question" # what the graph knows about the question
```

CLI installation is not required: `npx -y @nanonets/graft@0.28.1 build` works without global install. To enable for your own small English-language repository: `enabled = true` in section `[graft]` of `configs/ai_pobisk.toml`, then `graft build` in it. Without CLI or without graph the prompt goes as is — this is not an error, but degradation.

Two honest implementation details: `has_graph()` checks for `graft/.graph/wiring.json` or `graft/INDEX.md`, not just a directory named `graft` (otherwise status lied "built" with missing CLI); the map is counted once per task and cached — otherwise two subprocesses per retry iteration; `ask` is not called for Cyrillic queries.

---

## Auto-registration: AI Fills the Form, Mail Brings the Code

A task with no API: the site asks for name, email, password, "accept terms", and then sends a code. A script with selectors dies on the first redesign, so the decision is made by the model, not by an `xpath` list. Four modules:

| Module | What it does | Dependencies |
|---|---|---|
| `src/mailbox.py` | IMAP: find letter, extract code or link | stdlib (`imaplib`, `email`) |
| `src/pageview.py` | HTML → numbered list of controls `[f1]…` | stdlib (`html.parser`) |
| `src/browser.py` | hands: click, type, wait, screenshot | **Playwright** |
| `src/signup.py` | cycle: snapshot → decide → act → snapshot | — |

### Why Mail is Read Exactly This Way

A confirmation code is not "a number in the letter", but **a number next to a word**. Therefore `mailbox.py` does not return the first number it finds: it ranks candidates by distance to the hint word (`код`, `code`, `otp`, `подтвержд`), penalizes years (`2024`), prices (`1290 руб` — only if currency is glued to the number, not just mentioned in the letter), and phones. If the best candidate does not reach the threshold — it returns `None`, not "let it be this one". Mistake here means a blocked account, while "letter not arrived" is just a retry.

Three more properties more important than capabilities:

* **The folder is opened read-only** (`select(readonly=True)` + `BODY.PEEK[]`) — the agent does not mark your mail as read;
* **UID watermark.** Before pressing "register", `last_uid()` is remembered, then only letters with greater UID are searched. Otherwise an old code from a previous attempt would be taken as new;
* **Deadline waiting.** `wait_for_code` polls the inbox (`poll_sec`) and returns `None` on timeout — the registration cycle turns this into "human needed", not into substituting garbage.

### Connecting the Inbox (handyhost, domain igrasu.ru)

Server already verified: `mail.igrasu.ru:993` answers Dovecot, `IMAP4rev1`, `AUTH=PLAIN LOGIN`, has `IDLE` (polling can be replaced with push later, currently `poll_sec = 5` is enough).

```bash
# 1. Mailbox password — in the shared 0600 store, not in the repository and not in TOML.
python3 .agents/skills/_shared/secrets.py set imap_gh1 'password-of-the-box'

# 2. Check that mail entry and parsing work.
./ai_Pobisk.sh mail --lines 5
./ai_Pobisk.sh mail --from "no-reply" --days 2 --links igrasu.ru

# 3. Wait for a fresh code (watermark is taken at launch moment).
./ai_Pobisk.sh mail --wait 180 --code
```

Coordinates are in the `[mail]` section of `configs/ai_pobisk.toml` (`host/port/ssl/user/folder`); the password is only in env `AI_POBISK_IMAP_PASSWORD` or in the store under the name `password_secret = "imap_gh1"`. If the provider sends the code in a non-standard format, there is `code_regex` — one regular expression with a group, and it has priority over heuristics.

### Registration Cycle

```
page snapshot -> decision -> one action -> snapshot -> ...
```

Decides `signup.heuristic()` — rules table: first empty mandatory field, then consent checkbox, then button with word "create/register". The model is asked **only when the table is silent** (unfamiliar field, choice from options, "what next"). So a normal form closes without a single API call — and `--dry-run` honestly prints the plan, not emptiness.

Key separation: **the model decides where to put a value; the program decides what the value is.** Placeholders go into the prompt — `{{email}}`, `{{password}}`, `{{code}}`; the password never appears in the prompt text. An unresolvable placeholder (`{{code}}` when the letter has not arrived yet) is a stop, not an empty-string substitution.

What the agent **does not** do, but returns to the owner (`status: blocked`):

* CAPTCHA, SMS code, call, payment — bypassing CAPTCHA violates ToS and loses the arms race; an SMS code is physically unavailable;
* model confidence below `min_confidence`;
* model named a control that is not on the page.

Anti-loop protection — as in the supervisor: the same action three times or an unchanged page four steps in a row gives `loop_guard`, not an infinite loop.

```bash
cp configs/profile.example.toml configs/profile.toml   # who we are (gitignored)
./ai_Pobisk.sh signup https://site.ru/register          # dry run: prints plan
./ai_Pobisk.sh signup https://site.ru/register --yes    # actually clicks
./ai_Pobisk.sh signup https://site.ru/register --yes --headless --save-password
```

Account creation is an external and poorly reversible action, so it requires **two** consents: `allow_live = true` in the config **and** `--yes` on the command line. Without them the run stops after the first planned step.

Each step is written to `.ai_pobisk/signup/<site>-<time>.json`: what was on the page, what was decided, how it ended. Successful accounts — in `.ai_pobisk/accounts.json`; password (with `--save-password`) — in the `0600` store, never in the repository. Autonomous registration without such a trace — is not automation, it is an incident nobody can figure out.

The browser is the only non-stdlib dependency of the project, and it lives **inside the repository**, not in the system Python: `.venv/` for the package, `.playwright/` for Chromium itself. Both directories are in `.gitignore`, both are deleted together with the project — nothing remains on a foreign machine: neither a global package nor 300 MB in cache.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright .venv/bin/python -m playwright install chromium
```

After that `./ai_Pobisk.sh` itself takes `.venv/bin/python` (if it exists) and sets `PLAYWRIGHT_BROWSERS_PATH` itself — no manual variables are needed. Lazy import: `./ai_Pobisk.sh list` works on a machine without Playwright, and `signup` without it prints exactly these three commands instead of a traceback.

---

## Running Agents with Account and Model Rotation

Live agents — in `.agents/skills/`. Each has its own supervisor: it selects a working model itself, and when quotas run out — switches model, then account, then passes the task to another agent.

| Agent | Supervisor | Rotates | Models |
|---|---|---|---|
| **Cline** | `cline/cline-rotate.sh` | accounts × models | 25 |
| **OpenCode** | `opencode/opencode-rotate.sh` | accounts × models | 13 |
| **FreeBuff** | `freebuff/freebuff-rotate.sh` | accounts × models (by quota) | 16 |
| **NVIDIA NIM** | `nvidia/nvidia-rotate.sh` | models, **in parallel** | 11 |
| **Token Harbor** | `tokenharbor/tokenharbor-rotate.sh` | models | 4 |

```bash
cd /Users/michaelpopov/Documents/GitHub/_Different/AI/AI-Pobisk-Commander

# Cline — act mode, auto-approve
.agents/skills/cline/cline-rotate.sh "make a distribution of materials about the game"

# OpenCode — one-shot with auto-rotation
.agents/skills/opencode/opencode-rotate.sh -- "fix the failing test in src/auth.ts"

# FreeBuff — preflight by quota, then interactive TUI
.agents/skills/freebuff/freebuff-rotate.sh
.agents/skills/freebuff/freebuff-rotate.sh quota      # quota table, no spending

# NVIDIA NIM — ask a model
.agents/skills/nvidia/nvidia-rotate.sh ask "explain this diff"
.agents/skills/nvidia/nvidia-rotate.sh rpm            # budget: 11 models × 40 RPM

# Token Harbor
.agents/skills/tokenharbor/tokenharbor-rotate.sh ask "explain this diff"
```

### Regenerating Model Lists

```bash
.agents/skills/cline/cline_Models_Free_Make.sh
.agents/skills/opencode/opencode_Models_Free_Make.sh
.agents/skills/freebuff/FreeBuff_Models_Free_Make.sh
.agents/skills/nvidia/nvidia-rotate.sh probe           # which answer now
.agents/skills/tokenharbor/tokenharbor-rotate.sh models --refresh
```

All lists — **ids only, without names**, one per line.

---

## Common Infrastructure

### `.agents/skills/_shared/model-stats.py` — Model Health

One store `~/.ai-rotate/model-stats.json` for all skills: a model that one agent recognized as dead is not probed by another.

- **Stability Score** — `0.30·p95 + 0.30·jitter + 0.20·spike + 0.20·uptime` (formula from [free-coding-models](https://github.com/vava-nessa/free-coding-models), caps raised for slow CLI probes). Average latency lies: a model with average 250 ms but p95 6 s is ranked **below** stable 400 ms.
- **Circuit breaker** — 3 failures in a row → cooldown 900 s. `auth_error` (401/403/bad key) is parked forever: retry cannot fix it.
- **Rotation order** — healthy (by stability) → untested → cooling.
- **Family failover** — a dead `deepseek/*` moves to another `deepseek/*`, not a random model: mid-session the model character is preserved.

```bash
S=.agents/skills/_shared/model-stats.py
python3 "$S" show                 # table: state, stability, p95, uptime
python3 "$S" show cline           # one skill
python3 "$S" reset cline model    # forget a model
```

Disable: `CLINE_NO_STATS=1`, `OC_NO_STATS=1`, `FB_NO_STATS=1`, `QWEN_NO_STATS=1`.

### `.agents/skills/_shared/secrets.py` — API Keys

One file `~/.ai-rotate/secrets.json` (**chmod 600, outside the repository**). Keys are not in the repository, and `check` complains if they appear there.

```bash
S=.agents/skills/_shared/secrets.py
python3 "$S" set nvidia "nvapi-..."       # add/replace
python3 "$S" list                          # nvap…-ddy (70 chars) — masked
python3 "$S" get nvidia                    # full value, for scripts
python3 "$S" check                         # permissions + repo scan for keys
python3 "$S" fix-perms                     # chmod 600 after accidental 644
```

With a file weaker than 600 the script **refuses to work** (exit 2) — it will not hand out keys from a world-readable file. The scanner looks for full keys, not prefixes, so `AI-Pobisk-Commander` (contains `sk-`) does not cause false positives.

Scripts read the key this way, printing nothing:

```bash
. "$(dirname "$0")/../_shared/require-secret.sh"
require_secrets NVIDIA_API_KEY:nvidia       # exit 2 with a hint if missing
```

More details: [`_shared/SECRETS.md`](.agents/skills/_shared/SECRETS.md), [`_shared/README.md`](.agents/skills/_shared/README.md).

---

## Important to Know

**NVIDIA: limit 40 RPM per model, not per key.** Therefore `parallel` is the key mode: 11 models give 440 RPM aggregate vs 40 with sequential iteration.

**Catalogs lie.** NVIDIA `/v1/models` returns 82 ids — 11 answer, the rest get 404 "Function not found". Token Harbor: `th-orchestra` shows zero price, but answers **402**; the reliable sign of free is the `:free` suffix.

**Silence is also a refusal.** Rotation does not wait for supervisor's 30 minutes: `cline-rotate.sh` and `opencode-rotate.sh` check every 30 seconds whether the worker session is growing, and if nothing changes for two minutes — they remove the call and take the next model on the same account. The window and step are configurable: `CLINE_STALL_WINDOW`/`OC_STALL_WINDOW` (120 s) and `*_STALL_CHECK` (30 s).

**Slow ≠ dead.** `z-ai/glm-5.3-flash` and `moonshotai/kimi-k3` answer in 60–130 s. A short timeout (`NVIDIA_TIMEOUT`, default 240) would take them for broken.

Details and pitfalls — in `SKILL.md` of each agent.

---

## Windows Support

`ai_Pobisk` is written to run cross-platform. All paths use `os.path.join` / `pathlib`, and platform-specific settings live in `configs/ai_pobisk.toml` rather than in the code. To run on Windows:

1. Install Python 3.11+ and Playwright:
   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   $env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.playwright"
   .venv\Scripts\python.exe -m playwright install chromium
   ```
2. Run via `python src/main.py` or `bash ai_Pobisk.sh` (Git Bash / WSL).
3. Edit `configs/ai_pobisk.toml` for paths, timeouts, and parallelism instead of editing source code.

Platform-specific knobs live in `[paths]`, `[runtime]`, and `[browser]` sections of `configs/ai_pobisk.toml`.

---

## Repository Layout

```
AI-Pobisk-Commander/
├── ai_Pobisk.sh              # bash wrapper → src/main.py
├── README.md                 # this file (English)
├── ReadMe_RU.md              # русская версия
├── configs/                  # TOML configuration
│   ├── ai_pobisk.toml        # main configuration (paths, budgets, workers, mail)
│   ├── models_for_coding.toml # model rating for coding tasks
│   ├── profile.example.toml  # registration profile example
│   ├── profile.toml          # personal data (gitignored)
│   └── tasks/                # task markdown files (moved to configs/disabled/ when done)
├── src/                      # Python logic
│   ├── main.py               # entry point / CLI
│   ├── supervisor.py         # task supervisor loop
│   ├── statemachine.py       # DRAKON state machine
│   ├── config.py             # configuration loading
│   ├── classify.py           # outcome classification
│   ├── dispatcher.py         # task discovery
│   ├── journal.py            # journal + state
│   ├── metrics.py            # events + aggregates
│   ├── meta.py               # meta-model decisions
│   ├── graftctx.py           # code-graph context
│   ├── handlers.py           # hands (confirmations, questions)
│   ├── registry.py           # skill registry
│   ├── mailbox.py            # IMAP mailbox
│   ├── signup.py             # registration form filler
│   ├── browser.py            # Playwright driver
│   └── pageview.py           # HTML → controls
├── .agents/skills/           # agent skills (cline, opencode, nvidia, tokenharbor, freebuff)
├── .agents/skills/_shared/   # shared: model-stats, secrets, outcome
├── scripts/                  # auxiliary scripts
├── tests/                    # Playwright and Python tests
├── .ai_pobisk/               # runtime state (gitignored)
│   ├── events.jsonl
│   ├── journal.md
│   ├── state.json
│   └── status.json
├── docs/
│   └── machine.svg           # DRAKON silhouette
├── .venv/                    # Python venv (gitignored)
├── .playwright/              # Chromium binaries (gitignored)
└── .gitignore
```

---

## License

MIT — see [`LICENSE`](LICENSE).