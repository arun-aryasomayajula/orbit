#!/usr/bin/env python3
"""The one reader for a target's `.autopilot/.secrets.env` (spec decision 10).

PM tokens live in that file (0600, gitignored). Four callers need the same
bytes to mean the same thing: run.sh's parse loop (bash), command_center's
Settings > Secrets panel, doctor's token_env check, and prd_flow's stage
children. Three Python copies of the parser had already drifted, so this
module is the single definition and the other two delegate to it.

Format (byte-for-byte what run.sh's loop accepts): one KEY=VALUE per line,
blank lines and `#` comments skipped, split on the FIRST `=`, key stripped,
value taken verbatim. The file is PARSED, never sourced/executed - a value
containing shell metacharacters is an inert literal string here too.

Stdlib only. Never log or print what this returns.
"""
import os

SECRETS_NAME = ".secrets.env"


def path(target):
    """Where the file lives for `target` (existence not implied)."""
    return os.path.join(target, ".autopilot", SECRETS_NAME)


def read(target):
    """Parse `<target>/.autopilot/.secrets.env` -> {NAME: value}. A missing or
    unreadable file is not an error - it yields {} (no secrets configured)."""
    return read_path(path(target))


def read_path(p):
    """Same parse, for callers that already know the file's location (the
    dashboard resolves it from AP_HOME, which need not sit under the repo)."""
    out = {}
    try:
        with open(p) as f:
            text = f.read()
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v
    return out
