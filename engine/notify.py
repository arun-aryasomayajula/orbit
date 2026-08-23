#!/usr/bin/env python3
"""notify.py — best-effort operator notifications for the autopilot loop.

macOS notification (osascript) always; Slack too if a webhook is configured.
Never raises, never blocks the loop: every path is wrapped and short-timeout.
An identical title+body sent again within 60s is dropped (best-effort dedupe,
state under $AP_STATE) so a flapping caller doesn't spam the operator.

Usage:  notify.py "<title>" "<message>" ["<url>"]
        The optional url (usually the dashboard) is appended as a Slack link —
        the human gate should be one tap away from every event.
Config: optional Slack incoming-webhook URL in $AP_HOME/.slack_webhook
        (one line, git-ignored). No webhook file → macOS-only.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

# Where the optional Slack webhook file lives. Best-effort/never-fatal: prefer
# AP_HOME (operator config), fall back to AP_STATE, then this script's dir — so
# notify never blocks the loop even if the env is unset.
_CONFIG_DIR = (os.environ.get("AP_HOME") or os.environ.get("AP_STATE")
               or os.path.dirname(os.path.abspath(__file__)))
WEBHOOK_FILE = os.path.join(_CONFIG_DIR, ".slack_webhook")

# Dedupe state: prefer AP_STATE (per the brief - it's the loop's per-target
# scratch dir, so two targets sharing an AP_HOME never share a dedupe file),
# falling back the same way _CONFIG_DIR does when AP_STATE isn't set.
_STATE_DIR = (os.environ.get("AP_STATE") or os.environ.get("AP_HOME")
              or os.path.dirname(os.path.abspath(__file__)))
DEDUPE_FILE = os.path.join(_STATE_DIR, ".notify-last")
DEDUPE_WINDOW_SEC = 60


def notify_macos(title: str, message: str):
    # Post a native macOS notification via osascript (no extra deps).
    try:
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def notify_slack(title: str, message: str, url: str = ""):
    # POST to the configured Slack incoming webhook, if any (best-effort).
    # `hook` (the secret webhook) and `url` (the user-facing dashboard link)
    # must never mix — embedding the hook in the message would publish it.
    try:
        hook = open(WEBHOOK_FILE).read().strip()
    except OSError:
        return
    if not hook.startswith("https://hooks.slack.com/"):
        return
    try:
        text = f"*{title}*\n{message}" + (f"\n<{url}|Open the dashboard>" if url else "")
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(hook, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _is_duplicate(title: str, message: str) -> bool:
    # Best-effort: an identical title+body sent again inside the window is a
    # duplicate. url is deliberately excluded from the key (brief: "identical
    # title+body") — the same event re-notified with a fresher link is still
    # the same event. Never raises: a missing or corrupt state file just
    # means "not a duplicate" (the safe direction — send it), and a write
    # that fails leaves the next call free to dedupe against whatever state
    # already exists, or to send too, either way never blocking the caller.
    now = time.time()
    # errors="replace", not the strict default: a lone surrogate in title or
    # message (garbled input from wherever the caller sourced it) would
    # otherwise raise UnicodeEncodeError here, outside every try/except in
    # this function - breaking the "never raises" contract the whole module
    # promises for a mis-encoded string, not just a missing state file.
    digest = hashlib.sha256(f"{title}\n{message}".encode(errors="replace")).hexdigest()
    is_dup = False
    try:
        with open(DEDUPE_FILE) as f:
            prev = json.load(f)
        if (isinstance(prev, dict) and prev.get("hash") == digest
                and (now - float(prev.get("at", 0))) < DEDUPE_WINDOW_SEC):
            is_dup = True
    except Exception:
        pass
    try:
        tmp = f"{DEDUPE_FILE}.tmp{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"hash": digest, "at": now}, f)
        os.replace(tmp, DEDUPE_FILE)
    except Exception:
        pass
    return is_dup


def send(title: str, message: str, url: str = ""):
    # Fan out to every configured channel; failures are silent by design.
    # ORBIT_NO_NOTIFY (any non-empty value) short-circuits entirely, before
    # either channel runs — the one choke point for test suites (or any
    # harness) that want to opt out of real desktop/Slack notifications
    # without touching call sites.
    if os.environ.get("ORBIT_NO_NOTIFY"):
        return
    if _is_duplicate(title, message):
        return
    notify_macos(title, message)
    notify_slack(title, message, url)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: notify.py <title> <message> [url]", file=sys.stderr)
        sys.exit(2)
    send(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
