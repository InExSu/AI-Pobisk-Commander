"""adapters.py — registry of every skill, in dispatch order.

Order encodes preference for *work*: skills that can take a prompt come
first; skills that cannot (freebuff: TUI only) are capacity probes.

The supervisor walks this list; --list shows it with live availability.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.join(os.path.dirname(_HERE), ".agents", "skills", "_shared")
for p in (_HERE, _SHARED, os.path.join(_HERE, "adapters")):
    if p not in sys.path:
        sys.path.insert(0, p)

from adapters.base import Adapter           # noqa: E402
from adapters.cline import ClineAdapter     # noqa: E402
from adapters.freebuff import FreeBuffAdapter   # noqa: E402
from adapters.nvidia import NvidiaAdapter   # noqa: E402
from adapters.opencode import OpenCodeAdapter   # noqa: E402
from adapters.tokenharbor import TokenHarborAdapter  # noqa: E402

# Dispatch order: prompt-capable workers first, TUI-only last.
CLASSES = [
    NvidiaAdapter,        # pure HTTP, no accounts, most reliable
    TokenHarborAdapter,   # pure HTTP, 4 models
    OpenCodeAdapter,      # account+model rotation, needs isolated accounts
    ClineAdapter,         # account+model rotation, sweeps internally
    FreeBuffAdapter,      # TUI only: capacity probe
]

# Skills that can actually execute a prompt.
WORKERS = {"nvidia", "tokenharbor", "opencode", "cline"}


def build(repo_root):
    """Instantiate every adapter against the repo root."""
    return [cls(repo_root) for cls in CLASSES]


def by_name(repo_root, name):
    for a in build(repo_root):
        if a.skill == name:
            return a
    return None


def workers(repo_root):
    return [a for a in build(repo_root) if a.skill in WORKERS and a.available()]
