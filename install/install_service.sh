#!/bin/bash
# Install Orbit as a background service for a target repo - one unit PER
# CONCURRENCY SLOT (config `concurrency`, read via `config.py shellenv` as
# ORBIT_CONCURRENCY). macOS -> launchd (RunAtLoad + KeepAlive) agents.
# Linux -> systemd --user units. Each unit gets ORBIT_SLOT/ORBIT_SLOTS baked
# into its environment so engine/run.sh's slot identity (see run.sh's
# "slot identity" comment) is set without the operator exporting anything.
#
# Naming continuity: slot 0 keeps the LEGACY label/unit name
# (`com.orbit.orbit-<slug>` / `orbit-<slug>.service`) so an existing
# single-slot install upgrades in place instead of orphaning itself - the
# dashboard/launcher already read that name (engine/launcher.py's
# launchd_label()). Slots 1..N-1 get a `-slotN` suffix.
#
# Re-running reconciles: installs any slot 0..N-1 missing a unit, and removes
# units for slots >= N left over from a higher `concurrency` (matched by this
# target's exact slug under the `com.orbit.` prefix - never touches another
# target's service or a non-orbit unit).
#
# Usage:
#   install_service.sh <target-repo> <orbit-home>
#   install_service.sh <target-repo> <orbit-home> --uninstall
#   install_service.sh <target-repo> <orbit-home> --generate-only <dir>
#     writes the unit files (both plist AND systemd - host-independent, so
#     this mode is a pure test seam) into <dir> without touching
#     launchctl/systemctl. Reconcile add/remove still applies against <dir>.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: install_service.sh <target-repo> <orbit-home> [--uninstall | --generate-only <dir>]" >&2; exit 2; }
TARGET="$(cd "$1" && pwd)"; ORBIT_HOME="$(cd "$2" && pwd)"
shift 2

MODE="install"
GEN_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) MODE="uninstall"; shift ;;
    --generate-only)
      [ $# -ge 2 ] || { echo "--generate-only needs a directory" >&2; exit 2; }
      MODE="generate-only"; GEN_DIR="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# tr -c would also translate the trailing newline into a dash - strip non-alnum then trim
SLUG="orbit-$(basename "$TARGET" | tr -c 'a-zA-Z0-9' '-' | sed 's/-*$//')"
RUN="$ORBIT_HOME/engine/run.sh"
LOG="$TARGET/.autopilot/state/logs"
mkdir -p "$LOG"

# Concurrency -> slot count. Uninstall doesn't need it (removes everything
# matching regardless of what the config currently says - the workspace may
# be mid-teardown). Same 1..32 clamp contract as run.sh/config.py
# (config.py's CONCURRENCY_MAX); this becomes a fifth enforcement point of it.
SLOTS=1
if [ "$MODE" != "uninstall" ]; then
  eval "$(python3 "$ORBIT_HOME/engine/config.py" shellenv "$TARGET")" || { echo "config load failed"; exit 2; }
  SLOTS="${ORBIT_CONCURRENCY:-1}"
  case "$SLOTS" in ''|*[!0-9]*) SLOTS=1 ;; esac
  [ "$SLOTS" -ge 1 ] || SLOTS=1
  [ "$SLOTS" -le 32 ] || SLOTS=32
fi

# label/unit name for a given slot - slot 0 is the LEGACY name (see header).
label_for() { if [ "$1" -eq 0 ]; then echo "com.orbit.$SLUG"; else echo "com.orbit.$SLUG-slot$1"; fi; }
unit_for()  { if [ "$1" -eq 0 ]; then echo "$SLUG.service"; else echo "$SLUG-slot$1.service"; fi; }

# Slot numbers with a unit file already on disk for THIS slug, found by
# scanning filenames (never launchctl/systemctl - keeps reconcile testable
# and independent of the daemon being reachable). Dedups plist+service
# hits for the same slot (relevant only in --generate-only, which writes
# both). Guarded to `com.orbit.<this exact slug>` / `<this exact slug>` so a
# neighboring target's service, or any non-orbit unit, is never matched.
existing_slots() {  # existing_slots <dir>
  local dir="$1" f base n
  [ -d "$dir" ] || return 0
  {
    for f in "$dir"/com.orbit."$SLUG".plist "$dir"/com.orbit."$SLUG"-slot*.plist; do
      [ -e "$f" ] || continue
      base="$(basename "$f" .plist)"; base="${base#com.orbit.}"
      if [ "$base" = "$SLUG" ]; then echo 0
      else n="${base#"$SLUG"-slot}"; case "$n" in ''|*[!0-9]*) ;; *) echo "$n" ;; esac
      fi
    done
    for f in "$dir"/"$SLUG".service "$dir"/"$SLUG"-slot*.service; do
      [ -e "$f" ] || continue
      base="$(basename "$f" .service)"
      if [ "$base" = "$SLUG" ]; then echo 0
      else n="${base#"$SLUG"-slot}"; case "$n" in ''|*[!0-9]*) ;; *) echo "$n" ;; esac
      fi
    done
  } | sort -un
}

gen_plist() {  # gen_plist <slot> <outfile>
  local slot="$1" out="$2" label; label="$(label_for "$slot")"
  sed -e "s|__LABEL__|$label|g" -e "s|__RUN__|$RUN|g" -e "s|__TARGET__|$TARGET|g" \
      -e "s|__HOME__|$ORBIT_HOME|g" -e "s|__LOG__|$LOG|g" -e "s|__USERHOME__|$HOME|g" \
      -e "s|__SLOT__|$slot|g" -e "s|__SLOTS__|$SLOTS|g" \
      "$ORBIT_HOME/install/launchd.plist.tmpl" > "$out"
}
gen_systemd() {  # gen_systemd <slot> <outfile>
  local slot="$1" out="$2"
  sed -e "s|__RUN__|$RUN|g" -e "s|__TARGET__|$TARGET|g" -e "s|__HOME__|$ORBIT_HOME|g" \
      -e "s|__SLOT__|$slot|g" -e "s|__SLOTS__|$SLOTS|g" \
      "$ORBIT_HOME/install/systemd.service.tmpl" > "$out"
}

if [ "$MODE" = "generate-only" ]; then
  mkdir -p "$GEN_DIR"
  n=0
  while [ "$n" -lt "$SLOTS" ]; do
    label="$(label_for "$n")"; unit="$(unit_for "$n")"
    gen_plist "$n" "$GEN_DIR/$label.plist"
    gen_systemd "$n" "$GEN_DIR/$unit"
    echo "✓ generated $label.plist + $unit (slot $n/$SLOTS)"
    n=$((n + 1))
  done
  for n in $(existing_slots "$GEN_DIR"); do
    if [ "$n" -ge "$SLOTS" ]; then
      rm -f "$GEN_DIR/$(label_for "$n").plist" "$GEN_DIR/$(unit_for "$n")"
      echo "✓ removed stale slot $n - concurrency is now $SLOTS"
    fi
  done
  exit 0
fi

case "$(uname -s)" in
  Darwin)
    UNIT_DIR="$HOME/Library/LaunchAgents"; mkdir -p "$UNIT_DIR"
    if [ "$MODE" = "uninstall" ]; then
      for n in $(existing_slots "$UNIT_DIR"); do
        label="$(label_for "$n")"; plist="$UNIT_DIR/$label.plist"
        launchctl unload "$plist" 2>/dev/null || true
        rm -f "$plist"
        echo "✓ removed $label"
      done
      exit 0
    fi
    n=0
    while [ "$n" -lt "$SLOTS" ]; do
      label="$(label_for "$n")"; plist="$UNIT_DIR/$label.plist"
      gen_plist "$n" "$plist"
      launchctl unload "$plist" 2>/dev/null || true
      launchctl load "$plist"
      echo "✓ launchd agent $label loaded (slot $n/$SLOTS, RunAtLoad + KeepAlive). Logs: $LOG"
      n=$((n + 1))
    done
    for n in $(existing_slots "$UNIT_DIR"); do
      if [ "$n" -ge "$SLOTS" ]; then
        label="$(label_for "$n")"; plist="$UNIT_DIR/$label.plist"
        launchctl unload "$plist" 2>/dev/null || true
        rm -f "$plist"
        echo "✓ removed stale slot $n ($label) - concurrency is now $SLOTS"
      fi
    done
    echo "  stop:  $ORBIT_HOME/install/install_service.sh $TARGET $ORBIT_HOME --uninstall"
    ;;
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
    if [ "$MODE" = "uninstall" ]; then
      for n in $(existing_slots "$UNIT_DIR"); do
        unit="$(unit_for "$n")"
        systemctl --user disable --now "$unit" 2>/dev/null || true
        rm -f "$UNIT_DIR/$unit"
        echo "✓ removed $unit"
      done
      systemctl --user daemon-reload 2>/dev/null || true
      exit 0
    fi
    n=0
    while [ "$n" -lt "$SLOTS" ]; do
      gen_systemd "$n" "$UNIT_DIR/$(unit_for "$n")"
      n=$((n + 1))
    done
    for n in $(existing_slots "$UNIT_DIR"); do
      if [ "$n" -ge "$SLOTS" ]; then
        unit="$(unit_for "$n")"
        systemctl --user disable --now "$unit" 2>/dev/null || true
        rm -f "$UNIT_DIR/$unit"
        echo "✓ removed stale slot $n ($unit) - concurrency is now $SLOTS"
      fi
    done
    systemctl --user daemon-reload
    n=0
    while [ "$n" -lt "$SLOTS" ]; do
      systemctl --user enable --now "$(unit_for "$n")"
      n=$((n + 1))
    done
    echo "✓ systemd --user units enabled + started for $SLOTS slot(s). Logs: journalctl --user -u $SLUG -f"
    echo "  stop:  $ORBIT_HOME/install/install_service.sh $TARGET $ORBIT_HOME --uninstall"
    ;;
  *) echo "unsupported OS $(uname -s) - run each slot manually: ORBIT_SLOT=<n> ORBIT_SLOTS=$SLOTS ORBIT_HOME=$ORBIT_HOME $RUN $TARGET"; exit 1 ;;
esac
