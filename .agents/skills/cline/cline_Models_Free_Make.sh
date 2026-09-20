#!/usr/bin/env bash
# cline-free — extract every free model the "Cline Usage-Billing" (kilo)
# provider offers, and write it to cline-models-free.txt.
#
# The /models picker lists the Usage-Billing provider's catalogue. In the cline
# binary that catalogue is the models.dev provider table keyed `kilo`; its free
# tiers are marked by a `:free` suffix on the model id (plus the
# cline-free/* namespace and the OpenRouter free router).
#
# Sources, in order (first wins):
#   1. CLINE_BIN_OVERRIDE      explicit path to the real cline binary
#   2. cline from PATH         resolved, then the @cline/cli-* platform package
#                              it launches
#   3. CLINE_FREE_MODELS_JSON  escape hatch: a JSON file with the catalogue
#
# Output:  .agents/skills/cline/cline-models-free.txt   (id <TAB> name)
# Exit codes: 0 ok · 1 cline not found · 2 catalogue not found

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
[[ -L "${BASH_SOURCE[0]}" ]] && SKILL_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" >/dev/null 2>&1 && pwd)"

MODELS_FILE="$SKILL_DIR/cline-models-free.txt"

# ── Resolve the cline binary ──────────────────────────────────────────────────
CLINE_BIN="${CLINE_BIN_OVERRIDE:-}"
[[ -z "$CLINE_BIN" ]] && CLINE_BIN="$(command -v cline 2>/dev/null || true)"
if [[ -z "$CLINE_BIN" ]]; then
	echo "cline-free: cline binary not found on PATH" >&2
	echo "hint: CLINE_BIN_OVERRIDE=/path/to/cline $0" >&2
	exit 1
fi
CLINE_BIN="$(readlink -f "$CLINE_BIN" 2>/dev/null || echo "$CLINE_BIN")"

# The cline launcher is a thin shim; the real binary lives in the platform
# package next to it (@cline/cli-<platform>/bin/cline). Try the shim last, so a
# platform binary that actually contains the catalogue wins.
CANDIDATES=()
if [[ -f "$CLINE_BIN" ]]; then
	pkg_root="$(cd "$(dirname "$CLINE_BIN")/.." >/dev/null 2>&1 && pwd)"
	while IFS= read -r p; do
		[[ -n "$p" ]] && CANDIDATES+=("$p")
	done < <(ls -1 "$pkg_root"/node_modules/@cline/cli-*/bin/cline 2>/dev/null)
fi
CANDIDATES+=("$CLINE_BIN")

# ── Extract the free-model catalogue ──────────────────────────────────────────
catalogue_raw=""
if [[ -n "${CLINE_FREE_MODELS_JSON:-}" && -f "${CLINE_FREE_MODELS_JSON:-}" ]]; then
	catalogue_raw="$(cat "$CLINE_FREE_MODELS_JSON")"
else
	for bin in "${CANDIDATES[@]}"; do
		[[ -f "$bin" ]] || continue
		catalogue_raw="$(
			python3 - "$bin" <<'PY'
import json, re, sys
text = open(sys.argv[1], "rb").read().decode("utf-8", "replace")

FREE_PREFIX = "cline-free/"
FREE_SUFFIX = ":free"
ROUTER = "openrouter/free"
# Free tiers whose ids carry no marker. The cline table lists them free while the
# kilo (router) catalogue repeats the same id as a paid variant, so the marker
# test alone would miss them. Extend this set if a future build adds another.
FREE_IDS_WITHOUT_MARKER = {"z-ai/glm-5.3-flash"}


def provider_block(marker):
    """Return the models.dev provider table starting at `marker`, or ''."""
    i = text.find(marker)
    if i < 0:
        return ""
    seg = text[i:]
    depth, j = 0, 0
    while j < len(seg):
        c = seg[j]
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return seg[: j + 1]


def free_in(block):
    out = []
    for m in re.finditer(r'id:"([^"]+)",name:"([^"]*)"', block):
        mid, name = m.group(1), m.group(2)
        # Free membership is decided by the id, because that is what the CLI
        # pins with -m and what the daily cap is accounted against.
        is_free = (
            mid.startswith(FREE_PREFIX)
            or mid.endswith(FREE_SUFFIX)
            or mid == ROUTER
            # core free tiers whose ids do not carry the marker: they are
            # listed free in the cline table but appear as paid in the kilo one
            or mid in FREE_IDS_WITHOUT_MARKER
        )
        if is_free:
            out.append({"id": mid, "name": name})
    return out


# Two tables carry the /models list for "Cline Usage-Billing":
#   kilo  - the full router catalogue; free tiers end in :free
#   cline - the core free namespace (cline-free/* and the few free tiers that
#           live under another provider id, e.g. z-ai/glm-5.3-flash)
# The plain key `cline:{` also matches an unrelated empty object in the binary,
# so pin the start of the model id in the search marker.
# Scan the core table first: some of its ids (z-ai/glm-5.3-flash) also appear
# in the kilo catalogue as a *paid* variant and would be de-duplicated away if
# the router table were read first.
seen = set()
out = []
for marker in ('cline:{"cline-free/', 'kilo:{"'):
    for m in free_in(provider_block(marker)):
        if m["id"] not in seen:
            seen.add(m["id"])
            out.append(m)
print(json.dumps(out, ensure_ascii=False))
PY
		)"
		[[ -n "$catalogue_raw" && "$catalogue_raw" != "[]" ]] && break
	done
fi

if [[ -z "$catalogue_raw" || "$catalogue_raw" == "[]" ]]; then
	echo "cline-free: could not extract the free-model catalogue from cline" >&2
	echo "hint: CLINE_FREE_MODELS_JSON=/path/catalogue.json $0" >&2
	exit 2
fi

# ── Write the catalogue file atomically ───────────────────────────────────────
CATALOGUE_TMP="$(mktemp)"
printf '%s' "$catalogue_raw" >"$CATALOGUE_TMP"
python3 - "$CATALOGUE_TMP" "$MODELS_FILE" <<'PY'
import json, os, sys
models = json.load(open(sys.argv[1]))
path = sys.argv[2]
header = [
    "# Cline free models - auto-generated by cline_Models_Free_Make.sh",
    "# Source: the Usage-Billing (kilo) catalogue embedded in the cline binary.",
    "# Do not edit by hand; re-run cline_Models_Free_Make.sh after every cline update.",
    "# Format: model id <TAB> display name",
]
lines = [m["id"] + "\t" + m["name"] for m in models]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    f.write("\n".join(header) + "\n")
    f.write("\n".join(lines) + "\n")
os.replace(tmp, path)
print("wrote %d free models -> %s" % (len(lines), path), file=sys.stderr)
PY
rm -f "$CATALOGUE_TMP"

cat "$MODELS_FILE"
