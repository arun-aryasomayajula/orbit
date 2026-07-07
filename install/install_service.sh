#!/bin/bash
# Install Ratchet as a background service for a target repo.
# macOS → launchd (RunAtLoad + KeepAlive). Linux → systemd --user unit.
#   install_service.sh <target-repo> <ratchet-home>
set -euo pipefail
TARGET="$(cd "$1" && pwd)"; RATCHET_HOME="$(cd "$2" && pwd)"
# tr -c would also translate the trailing newline into a dash — strip non-alnum then trim
SLUG="ratchet-$(basename "$TARGET" | tr -c 'a-zA-Z0-9' '-' | sed 's/-*$//')"
RUN="$RATCHET_HOME/engine/run.sh"
LOG="$TARGET/.autopilot/state/logs"
mkdir -p "$LOG"

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.ratchet.$SLUG.plist"
    sed -e "s|__LABEL__|com.ratchet.$SLUG|g" \
        -e "s|__RUN__|$RUN|g" -e "s|__TARGET__|$TARGET|g" \
        -e "s|__HOME__|$RATCHET_HOME|g" -e "s|__LOG__|$LOG|g" \
        -e "s|__USERHOME__|$HOME|g" \
        "$RATCHET_HOME/install/launchd.plist.tmpl" > "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "✓ launchd agent com.ratchet.$SLUG loaded (RunAtLoad + KeepAlive). Logs: $LOG"
    echo "  stop:  launchctl unload $PLIST"
    ;;
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
    UNIT="$UNIT_DIR/$SLUG.service"
    sed -e "s|__RUN__|$RUN|g" -e "s|__TARGET__|$TARGET|g" -e "s|__HOME__|$RATCHET_HOME|g" \
        "$RATCHET_HOME/install/systemd.service.tmpl" > "$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable --now "$SLUG.service"
    echo "✓ systemd --user unit $SLUG.service enabled + started. Logs: journalctl --user -u $SLUG -f"
    echo "  stop:  systemctl --user stop $SLUG.service"
    ;;
  *) echo "unsupported OS $(uname -s) — run the loop manually: RATCHET_HOME=$RATCHET_HOME $RUN $TARGET"; exit 1 ;;
esac
