#!/usr/bin/env python3
"""secrets.py — one place for every API key the rotate skills use.

Store: ~/.ai-rotate/secrets.json, mode 0600, OUTSIDE any git repository.
Override the location with AI_ROTATE_DIR.

Shape:
    {
      "nvidia":      {"key": "nvapi-...", "label": "...", "added": epoch},
      "tokenharbor": {"key": "thk_live_...", ...},
      ...
    }

Rules this module enforces:
  * the file is created 0600 and refuses to read one that is group/world
    readable (fix with `secrets.py fix-perms`);
  * `get` never prints a key in full — `reveal` does, and only on request;
  * every listing masks keys by default.

Subcommands:
    set    <name> <key> [--label TEXT]   add or replace a key
    get    <name>                        print the key (for scripts; masked with --mask)
    reveal <name>                        print the key unmasked, explicitly
    list / ls                            table of names, masked keys, ages
    rm     <name>                        delete a key
    path                                 print the store path
    check                                verify perms + warn about anything loose
    fix-perms                            chmod 600 the store and its directory
    env    <name>                        print `export UPPER_NAME=key` for eval
    export                               print every key as export lines

Exit codes: 0 ok · 1 not found / usage · 2 store unreadable or insecure.
"""

import json
import os
import re
import stat
import sys
import time

# A real key, not a prefix. Prefix-only matching produces constant false
# positives: "AI-Pobisk-Commander" contains "sk-", and docs quote "nvapi-..."
# as a placeholder. Require a plausible-length opaque tail after the prefix.
SECRET_RE = re.compile(
    rb"nvapi-[A-Za-z0-9_-]{20,}"
    rb"|thk_live_[A-Za-z0-9_-]{20,}"
    rb"|sk-ant-[A-Za-z0-9_-]{20,}"
    rb"|sk-[A-Za-z0-9]{28,}"
    rb"|gsk_[A-Za-z0-9]{20,}"
    rb"|AIza[0-9A-Za-z_-]{30,}"
)

STORE_DIR = os.environ.get("AI_ROTATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".ai-rotate"
)
STORE = os.path.join(STORE_DIR, "secrets.json")

# Which env var each secret name maps to, for `env`/`export`.
ENV_NAMES = {
    "nvidia": "NVIDIA_API_KEY",
    "tokenharbor": "TOKENHARBOR_API_KEY",
    "baibai": "BAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "zai": "ZAI_API_KEY",
    "kilo": "KILO_API_KEY",
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "googleai": "GEMINI_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}


def mask(key):
    """First 4 + last 4, so a key stays recognisable but not reusable."""
    k = key or ""
    if len(k) <= 12:
        return "*" * len(k)
    return "%s…%s (%d chars)" % (k[:4], k[-4:], len(k))


def env_name(name):
    return ENV_NAMES.get(name, name.upper().replace("-", "_") + "_API_KEY")


def perms_ok(path):
    try:
        m = os.stat(path).st_mode
        return not (m & (stat.S_IRGRP | stat.S_IWGRP |
                         stat.S_IROTH | stat.S_IWOTH))
    except OSError:
        return True


def load():
    if not os.path.exists(STORE):
        return {}
    if not perms_ok(STORE):
        print("secrets: REFUSING to read %s: permissions too open.\n"
              "        run: %s fix-perms" % (STORE, sys.argv[0]),
              file=sys.stderr)
        sys.exit(2)
    try:
        with open(STORE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        print("secrets: cannot read %s: %s" % (STORE, e), file=sys.stderr)
        sys.exit(2)


def save(d):
    os.makedirs(STORE_DIR, exist_ok=True)
    try:
        os.chmod(STORE_DIR, 0o700)
    except OSError:
        pass
    fd = os.open(STORE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=2, sort_keys=True)
    finally:
        try:
            os.chmod(STORE, 0o600)
        except OSError:
            pass


def cmd_set(argv):
    if len(argv) < 2:
        print("usage: secrets.py set <name> <key> [--label TEXT]", file=sys.stderr)
        return 1
    name, key = argv[0], argv[1]
    label = ""
    if "--label" in argv:
        i = argv.index("--label")
        if i + 1 < len(argv):
            label = argv[i + 1]
    d = load()
    d[name] = {"key": key, "label": label or name,
               "added": d.get(name, {}).get("added", time.time()),
               "updated": time.time()}
    save(d)
    print("saved %s = %s" % (name, mask(key)))
    return 0


def cmd_get(argv, masked=False):
    if not argv:
        print("usage: secrets.py get <name> [--mask]", file=sys.stderr)
        return 1
    d = load()
    e = d.get(argv[0])
    if not e:
        print("secrets: no key named %r" % argv[0], file=sys.stderr)
        return 1
    print(mask(e["key"]) if masked or "--mask" in argv else e["key"])
    return 0


def cmd_list(_argv):
    d = load()
    if not d:
        print("no secrets stored (%s)" % STORE)
        return 0
    print("%-14s %-28s %-8s %s" % ("name", "key (masked)", "env var", "age"))
    for name in sorted(d):
        e = d[name]
        age = int(time.time() - (e.get("added") or 0))
        unit = "s"
        if age > 86400:
            age, unit = age // 86400, "d"
        elif age > 3600:
            age, unit = age // 3600, "h"
        elif age > 60:
            age, unit = age // 60, "m"
        print("%-14s %-28s %-8s %s%s" % (
            name, mask(e.get("key", "")), env_name(name), age, unit))
    return 0


def cmd_rm(argv):
    if not argv:
        print("usage: secrets.py rm <name>", file=sys.stderr)
        return 1
    d = load()
    if argv[0] not in d:
        print("secrets: no key named %r" % argv[0], file=sys.stderr)
        return 1
    del d[argv[0]]
    save(d)
    print("removed %s" % argv[0])
    return 0


def cmd_check(_argv):
    ok = True
    if not os.path.exists(STORE):
        print("store does not exist yet: %s" % STORE)
        return 0
    m = stat.S_IMODE(os.stat(STORE).st_mode)
    print("store:  %s  mode=%o" % (STORE, m))
    if not perms_ok(STORE):
        print("  !! INSECURE: readable by group/other — run fix-perms")
        ok = False
    else:
        print("  ok: owner-only")
    dm = stat.S_IMODE(os.stat(STORE_DIR).st_mode)
    print("dir:    %s  mode=%o" % (STORE_DIR, dm))
    # Warn about keys that are still sitting in a git repo somewhere.
    for rel in (".agents", ".", ".."):
        p = os.path.abspath(rel)
        if os.path.isdir(os.path.join(p, ".git")):
            hits = []
            for root, dirs, files in os.walk(p):
                dirs[:] = [x for x in dirs
                           if x not in (".git", "node_modules", "__pycache__")]
                for fn in files:
                    if not fn.endswith((".sh", ".py", ".json", ".md", ".txt",
                                        ".yaml", ".yml", ".env")):
                        continue
                    fp = os.path.join(root, fn)
                    try:
                        if os.path.getsize(fp) > 2_000_000:
                            continue
                        with open(fp, "rb") as f:
                            blob = f.read()
                        if SECRET_RE.search(blob):
                            hits.append(os.path.relpath(fp, p))
                    except OSError:
                        pass
            if hits:
                print("  !! possible plaintext keys inside git repo %s:" % p)
                for h in hits[:10]:
                    print("     %s" % h)
                ok = False
    return 0 if ok else 1


def cmd_fix(_argv):
    if os.path.exists(STORE):
        os.chmod(STORE, 0o600)
        print("chmod 600 %s" % STORE)
    if os.path.isdir(STORE_DIR):
        os.chmod(STORE_DIR, 0o700)
        print("chmod 700 %s" % STORE_DIR)
    return 0


def cmd_env(argv):
    d = load()
    names = argv if argv else sorted(d)
    for n in names:
        e = d.get(n)
        if not e:
            continue
        print("export %s=%s" % (env_name(n), e["key"]))
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == "set":
        return cmd_set(argv)
    if cmd == "get":
        return cmd_get(argv)
    if cmd in ("reveal", "show"):
        return cmd_get(argv)
    if cmd in ("list", "ls"):
        return cmd_list(argv)
    if cmd in ("rm", "del", "remove"):
        return cmd_rm(argv)
    if cmd == "path":
        print(STORE)
        return 0
    if cmd == "check":
        return cmd_check(argv)
    if cmd == "fix-perms":
        return cmd_fix(argv)
    if cmd in ("env", "export"):
        return cmd_env(argv)
    print("secrets: unknown subcommand %r" % cmd, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
