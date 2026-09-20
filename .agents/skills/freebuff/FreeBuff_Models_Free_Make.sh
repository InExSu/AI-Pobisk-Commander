#!/usr/bin/env bash
# FreeBuff_Models_Free_Make.sh — collect every model id the Freebuff free tier
# offers and write it to freebuff-models-free.txt.
#
# Freebuff has no `freebuff models` command and no public catalogue: the model
# list lives server-side and is only visible per account, in the (read-only)
# session API response that freebuff-rotate.sh already probes:
#
#   GET https://codebuff.com/api/v1/freebuff/session
#   Authorization: Bearer <authToken from the saved account file>
#
#   -> freebucks.prices        { "<model id>": <Freebucks per request> }
#   -> rateLimitsByModel       { "<model id>": {limit, recentCount, resetAt} }
#
# A model belongs to the free catalogue iff it appears in EITHER map:
#   - prices        every model the free tier can spend Freebucks on (all of
#                   them are free-tier models; unmetered ones such as
#                   z-ai/glm-5.3-flash are here but absent from rateLimits)
#   - rateLimits    every metered model (session quota per pacific day)
# The union across all saved accounts is taken, because an account's tier can
# hide a model from its own response.
#
# Sources, in order (first wins):
#   1. FB_FREE_MODELS_JSON   escape hatch: a JSON file with the catalogue
#                            (list of ids, or [{"id": ...}, ...])
#   2. the live session API  one read-only GET per saved account
#   3. FB_ACCOUNTS_DIR       accounts root (default: ~/.config/manicode/accounts)
#
# The probe is read-only: it never starts a session and never burns Freebucks.
#
# Output:  .agents/skills/freebuff/freebuff-models-free.txt   (id, one per line)
# Exit codes: 0 ok · 1 no usable accounts · 2 catalogue not found

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
[[ -L "${BASH_SOURCE[0]}" ]] && SKILL_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" >/dev/null 2>&1 && pwd)"

MANICODE_DIR="${FB_MANICODE_DIR:-$HOME/.config/manicode}"
ACCOUNTS_DIR="${FB_ACCOUNTS_DIR:-$MANICODE_DIR/accounts}"
MODELS_FILE="$SKILL_DIR/freebuff-models-free.txt"
PROBE_TIMEOUT="${FB_PROBE_TIMEOUT:-10}"

# ── Collect the model ids ─────────────────────────────────────────────────────
ids_raw=""
if [[ -n "${FB_FREE_MODELS_JSON:-}" && -f "${FB_FREE_MODELS_JSON:-}" ]]; then
	ids_raw="$(cat "$FB_FREE_MODELS_JSON")"
else
	[[ -d "$ACCOUNTS_DIR" ]] || {
		echo "FreeBuff-free: no accounts dir at $ACCOUNTS_DIR" >&2
		echo "hint: fbacc -> Extract session, or FB_ACCOUNTS_DIR=/path $0" >&2
		exit 1
	}
	ids_raw="$(FB_PROBE_TIMEOUT="$PROBE_TIMEOUT" python3 - "$ACCOUNTS_DIR" <<'PY'
import glob, json, os, sys, urllib.error, urllib.request

accounts_dir = sys.argv[1]
timeout = float(os.environ.get("FB_PROBE_TIMEOUT", "10"))
API = "https://codebuff.com/api/v1/freebuff/session"

files = sorted(
    p for p in glob.glob(os.path.join(accounts_dir, "*.json"))
    if os.path.basename(p) != "_index.json"
)

ids = {}          # id -> {"price": int|None, "metered": bool}
errors = 0
probed = 0
for path in files:
    try:
        creds = json.load(open(path))
    except Exception:
        continue
    token = creds.get("authToken")
    if not token:
        continue
    req = urllib.request.Request(
        API,
        headers={
            "Authorization": "Bearer " + token,
            "User-Agent": "freebuff-account-manager/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except Exception:
        errors += 1
        continue
    probed += 1

    fbk = d.get("freebucks") if isinstance(d, dict) else None
    prices = (fbk or {}).get("prices") or {}
    for mid, price in prices.items():
        e = ids.setdefault(mid, {"price": None, "metered": False})
        if isinstance(price, (int, float)):
            e["price"] = price

    rlm = d.get("rateLimitsByModel") or (d.get("session") or {}).get("rateLimitsByModel") or {}
    for mid in rlm:
        ids.setdefault(mid, {"price": None, "metered": False})["metered"] = True

if not ids:
    print(json.dumps({"ids": [], "probed": probed, "errors": errors}))
    sys.exit(0)

# Display order: cheapest first, then metered-only, then unpriced. Within the
# same price keep alphabetical — stable output, and the cheapest model is the
# one worth pinning first (freebuff-rotate.sh prefers FB_MODELS order anyway).
def sort_key(item):
    mid, e = item
    price = e["price"]
    return (0 if price is not None else 1, price if price is not None else 0, mid)

out = [mid for mid, _ in sorted(ids.items(), key=sort_key)]
print(json.dumps({"ids": out, "probed": probed, "errors": errors}, ensure_ascii=False))
PY
)"
fi

if [[ -z "$ids_raw" ]]; then
	echo "FreeBuff-free: could not collect the free-model catalogue" >&2
	echo "hint: FB_FREE_MODELS_JSON=/path/catalogue.json $0" >&2
	exit 2
fi

# ── Write the catalogue file atomically ───────────────────────────────────────
CATALOGUE_TMP="$(mktemp)"
printf '%s' "$ids_raw" >"$CATALOGUE_TMP"
python3 - "$CATALOGUE_TMP" "$MODELS_FILE" <<'PY'
import json, os, sys

raw = json.load(open(sys.argv[1]))
path = sys.argv[2]

if isinstance(raw, dict):
    items = raw.get("ids") or []
    probed, errors = raw.get("probed", 0), raw.get("errors", 0)
else:
    items = raw
    probed = errors = 0

ids = []
for it in items:
    mid = it["id"] if isinstance(it, dict) else it
    mid = str(mid).strip()
    if mid:
        ids.append(mid)
# de-dup, keep order
seen = set()
ids = [m for m in ids if not (m in seen or seen.add(m))]

if not ids:
    print("FreeBuff-free: catalogue empty - nothing written", file=sys.stderr)
    sys.exit(2)

header = [
    "# Freebuff free-tier models - auto-generated by FreeBuff_Models_Free_Make.sh",
    "# Source: GET https://codebuff.com/api/v1/freebuff/session (read-only, per saved account).",
    "# A model is in the catalogue iff it appears in freebucks.prices or rateLimitsByModel.",
    "# Sorted by Freebucks price per request, cheapest first.",
    "# Do not edit by hand; re-run FreeBuff_Models_Free_Make.sh to refresh.",
    "# Format: model id, one per line (no display names)",
]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    f.write("\n".join(header) + "\n")
    f.write("\n".join(ids) + "\n")
os.replace(tmp, path)
if probed:
    print("probed %d account(s), %d failed" % (probed, errors), file=sys.stderr)
print("wrote %d free models -> %s" % (len(ids), path), file=sys.stderr)
PY
rc=$?
rm -f "$CATALOGUE_TMP"
[[ $rc -eq 0 ]] || exit $rc

cat "$MODELS_FILE"
