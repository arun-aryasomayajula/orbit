#!/bin/bash
# Ratchet — one-command setup for a target repo.
#   ./install.sh <target-repo>
# Scaffolds <target>/.autopilot/ (auto-detects gates), installs the /ratchet-cycle
# command + agents into <target>/.claude/, symlinks the `ratchet` convenience script
# onto PATH, and offers to install the background service (launchd on macOS, systemd on Linux).
set -euo pipefail

RATCHET_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"
[ -n "$TARGET" ] || { echo "usage: ./install.sh <target-repo>"; exit 2; }
TARGET="$(cd "$TARGET" && pwd)"
[ -d "$TARGET/.git" ] || { echo "error: $TARGET is not a git repo"; exit 2; }

echo "Ratchet → $TARGET"
echo "1. scaffolding .autopilot/ profile"
python3 "$RATCHET_HOME/install/init.py" "$TARGET" "$RATCHET_HOME"

# 2. put `ratchet` on PATH (thin dispatcher — plain script, not a packaged CLI)
BINDIR="$HOME/.local/bin"; mkdir -p "$BINDIR"
ln -sf "$RATCHET_HOME/bin/ratchet" "$BINDIR/ratchet"
chmod +x "$RATCHET_HOME/bin/ratchet" "$RATCHET_HOME/engine/run.sh" 2>/dev/null || true
echo "2. linked \`ratchet\` → $BINDIR/ratchet  (ensure $BINDIR is on your PATH)"

echo "3. validating wiring"
RATCHET_HOME="$RATCHET_HOME" python3 "$RATCHET_HOME/engine/doctor.py" "$TARGET" || {
  echo; echo "   doctor found issues (usually: review gates in .autopilot/config.yaml). Fix, then re-run \`ratchet doctor $TARGET\`."; }

echo
read -r -p "4. install the background service now? [y/N] " ans
if [[ "$ans" =~ ^[Yy] ]]; then
  "$RATCHET_HOME/install/install_service.sh" "$TARGET" "$RATCHET_HOME"
else
  echo "   skipped. Later: \`ratchet install $TARGET\`, or run one cycle in the foreground with \`ratchet run $TARGET\`."
fi
echo "done."
