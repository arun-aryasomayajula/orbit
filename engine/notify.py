#!/usr/bin/env python3
"""notify.py — best-effort operator notifications for the autopilot loop.

macOS notification (osascript) always; Slack too if a webhook is configured.
Never raises, never blocks the loop: every path is wrapped and short-timeout.

Usage:  notify.py "<title>" "<message>"
Config: optional Slack incoming-webhook URL in $AP_HOME/.slack_webhook
        (one line, git-ignored). No webhook file → macOS-only.
"""
import json
import os
import subprocess
import sys
import urllib.request

# Where the optional Slack webhook file lives. Best-effort/never-fatal: prefer
# AP_HOME (operator config), fall back to AP_STATE, then this script's dir — so
# notify never blocks the loop even if the env is unset.
_CONFIG_DIR = (os.environ.get("AP_HOME") or os.environ.get("AP_STATE")
               or os.path.dirname(os.path.abspath(__file__)))
WEBHOOK_FILE = os.path.join(_CONFIG_DIR, ".slack_webhook")


def notify_macos(title: str, message: str):
    # Post a native macOS notification via osascript (no extra deps).
    try:
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def notify_slack(title: str, message: str):
    # POST to the configured Slack incoming webhook, if any (best-effort).
    try:
        url = open(WEBHOOK_FILE).read().strip()
    except OSError:
        return
    if not url.startswith("https://hooks.slack.com/"):
        return
    try:
        body = json.dumps({"text": f"*{title}*\n{message}"}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def send(title: str, message: str):
    # Fan out to every configured channel; failures are silent by design.
    notify_macos(title, message)
    notify_slack(title, message)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: notify.py <title> <message>", file=sys.stderr)
        sys.exit(2)
    send(sys.argv[1], sys.argv[2])
