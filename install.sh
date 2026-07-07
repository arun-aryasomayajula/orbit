#!/bin/bash
# Orbit — one-command setup for a target repo.
#   ./install.sh <target-repo>
# Scaffolds <target>/.autopilot/ (auto-detects gates), installs the /orbit-cycle
# command + agents into <target>/.claude/, symlinks the `orbit` convenience script
# onto PATH, and offers to install the background service (launchd on macOS, systemd on Linux).
set -euo pipefail

ORBIT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"
[ -n "$TARGET" ] || { echo "usage: ./install.sh <target-repo>"; exit 2; }
TARGET="$(cd "$TARGET" && pwd)"
[ -d "$TARGET/.git" ] || { echo "error: $TARGET is not a git repo"; exit 2; }

echo "Orbit → $TARGET"
echo "1. scaffolding .autopilot/ profile"
python3 "$ORBIT_HOME/install/init.py" "$TARGET" "$ORBIT_HOME"

# 2. put `orbit` on PATH (thin dispatcher — plain script, not a packaged CLI)
BINDIR="$HOME/.local/bin"; mkdir -p "$BINDIR"
ln -sf "$ORBIT_HOME/bin/orbit" "$BINDIR/orbit"
chmod +x "$ORBIT_HOME/bin/orbit" "$ORBIT_HOME/engine/run.sh" 2>/dev/null || true
echo "2. linked \`orbit\` → $BINDIR/orbit  (ensure $BINDIR is on your PATH)"

echo "3. validating wiring"
ORBIT_HOME="$ORBIT_HOME" python3 "$ORBIT_HOME/engine/doctor.py" "$TARGET" || {
  echo; echo "   doctor found issues (usually: review gates in .autopilot/config.yaml). Fix, then re-run \`orbit doctor $TARGET\`."; }

echo
read -r -p "4. install the background service now? [y/N] " ans
if [[ "$ans" =~ ^[Yy] ]]; then
  "$ORBIT_HOME/install/install_service.sh" "$TARGET" "$ORBIT_HOME"
else
  echo "   skipped. Later: \`orbit install $TARGET\`, or run one cycle in the foreground with \`orbit run $TARGET\`."
fi
echo "done."
