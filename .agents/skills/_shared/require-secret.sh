#!/usr/bin/env bash
# require-secret — load an API key from the shared secret store into the env.
#
#   . "$(dirname "$0")/../_shared/require-secret.sh"     # source, not execute
#   require_secret NVIDIA_API_KEY nvidia                 # env var <- store name
#
# On success the variable is exported and its value is NEVER echoed. On failure
# the script exits 2 with a hint. Nothing is written to disk.
#
# Source of truth: ~/.ai-rotate/secrets.json (mode 0600, outside git), managed
# by _shared/secrets.py. This file exists so every skill reads keys the same
# way: one store, one lookup, no keys in configs or in the repo.

# Resolve _shared/ next to the sourcing script, so it works from any skill dir.
_SECRET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# require_secret <ENV_VAR> <STORE_NAME>
# Prints nothing on success (silence is the point: no key can leak into logs).
require_secret() {
  local var_name="$1" store_name="${2:-}"
  [[ -z "$store_name" ]] && store_name="$(printf '%s' "$var_name" | tr '[:upper:]' '[:lower:]')"

  # Already set by the caller (CI, a wrapper, an export) — respect it.
  if [[ -n "${!var_name:-}" ]]; then
    return 0
  fi

  local py="$_SECRET_DIR/secrets.py"
  [[ -f "$py" ]] || {
    printf 'require-secret: %s not found\n' "$py" >&2
    return 2
  }

  local val rc
  val="$(python3 "$py" get "$store_name" 2>/dev/null)"
  rc=$?
  if [[ $rc -ne 0 || -z "$val" ]]; then
    printf 'require-secret: no API key for %s in the secret store\n' "$store_name" >&2
    printf '  add it:  %s set %s <key>\n' "$py" "$store_name" >&2
    return 2
  fi

  export "$var_name=$val"
  return 0
}

# Convenience: load several at once. Usage: require_secrets NVIDIA_API_KEY:nvidia ...
# Exits the whole script on the first miss (use when the key is mandatory).
require_secrets() {
  local spec var_name store_name
  for spec in "$@"; do
    var_name="${spec%%:*}"
    store_name="${spec##*:}"
    if ! require_secret "$var_name" "$store_name"; then
      exit 2
    fi
  done
}
