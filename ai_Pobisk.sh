#!/usr/bin/env bash
# ai_Pobisk — supervisor that runs configs/tasks with .agents/skills.
#
#   ./ai_Pobisk.sh                      # all tasks
#   ./ai_Pobisk.sh --task "01 добавка.md"
#   ./ai_Pobisk.sh --dry-run            # plan only, no model called
#   ./ai_Pobisk.sh list                 # discovered tasks
#   ./ai_Pobisk.sh skills [--probe]     # adapters, health, optional ping
#   ./ai_Pobisk.sh self-test            # meta model + adapters + machine
#   ./ai_Pobisk.sh status [--live] [--json]
#   ./ai_Pobisk.sh watch [--json]       # live view during a run
#   ./ai_Pobisk.sh tail [--follow]      # last events, like tail -f
#   ./ai_Pobisk.sh check [--notify-cmd CMD]
#   ./ai_Pobisk.sh serve [--port N]     # /metrics for Prometheus
#   ./ai_Pobisk.sh report [--run ID]    # post-hoc breakdown
#   ./ai_Pobisk.sh drakon [--svg PATH]  # control machine + DRAKON silhouette
#   ./ai_Pobisk.sh graft [build|ask]    # code-graph context
#
# Why bash: it resolves the repo root and execs python. All logic lives in
# src/. Adding a key before launch works as usual:
#   python3 .agents/skills/_shared/secrets.py set nvidia "nvapi-..."

set -uo pipefail

SOURCE="${BASH_SOURCE[0]}"
# Resolve through symlinks so a PATH-installed wrapper still finds src/.
while [[ -L "$SOURCE" ]]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
REPO_ROOT="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"

export AI_POBISK_ROOT="$REPO_ROOT"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || {
  echo "ai_Pobisk: python3 not found (set PYTHON=/path/to/python3)" >&2
  exit 1
}

exec "$PY" "$REPO_ROOT/src/main.py" "$@"
