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
# Naming COLLISION between DIFFERENT targets: a target's derived
# `<slug>-slotN` filename can be byte-identical to a different target's own
# LEGACY filename (e.g. target dir `app`'s slot-1 unit is named exactly
# like target dir `app-slot1`'s slot-0 unit) - the slug-plus-suffix scheme
# alone cannot tell those apart. Every unit this script writes bakes in
# ORBIT_TARGET, so before writing OR removing ANY file matched by this
# target's naming pattern, the script reads that file's ORBIT_TARGET back
# and compares it to THIS target's path:
#   - write: an existing file belonging to a DIFFERENT target is never
#     overwritten - the write is REFUSED (loud error, nonzero exit) instead.
#     Two units cannot share one filename, so this is a real, unresolvable
#     conflict; the operator must rename one of the two target directories.
#   - remove (reconcile-down / uninstall): a foreign-owned file is left
#     alone with a loud warning, never deleted.
# A file with no readable ORBIT_TARGET (not one of ours, or unreadable) is
# treated as foreign for both purposes.
#
# Re-running reconciles: installs any slot 0..N-1 missing a unit, and
# removes units for slots >= N left over from a higher `concurrency`
# (subject to the ownership check above).
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
FAILED=0   # set to 1 when a slot's WRITE is refused for belonging to another target

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

# The ORBIT_TARGET baked into an existing plist or systemd unit file, or
# empty if the file is absent/unreadable/doesn't have one.
unit_owner() {  # unit_owner <file>
  local f="$1"
  case "$f" in
    *.plist)   sed -n 's|.*<key>ORBIT_TARGET</key><string>\(.*\)</string>.*|\1|p' "$f" 2>/dev/null | head -1 ;;
    *.service) sed -n 's|^Environment=ORBIT_TARGET=||p' "$f" 2>/dev/null | head -1 ;;
  esac
}

# true (0) if $1 does not exist yet, OR exists and already belongs to THIS
# target (its baked-in ORBIT_TARGET equals $TARGET) - i.e. safe to (over)write.
ours_or_absent() {
  local f="$1" owner
  [ -e "$f" ] || return 0
  owner="$(unit_owner "$f")"
  [ -n "$owner" ] && [ "$owner" = "$TARGET" ]
}

# true (0) if $1 exists AND belongs to THIS target - i.e. safe to delete/unload.
ours() {
  local f="$1" owner
  [ -e "$f" ] || return 1
  owner="$(unit_owner "$f")"
  [ -n "$owner" ] && [ "$owner" = "$TARGET" ]
}

# Write $3 via generator function $2 into path $1, but only if ours_or_absent
# ($1) - otherwise refuse (loud error, FAILED=1) rather than clobber a
# different target's unit that happens to share this filename.
write_guarded() {  # write_guarded <path> <gen-fn> <slot>
  local path="$1" fn="$2" slot="$3"
  if ours_or_absent "$path"; then
    "$fn" "$slot" "$path"
    return 0
  fi
  echo "✗ refusing to write $path (slot $slot): it belongs to a DIFFERENT target ($(unit_owner "$path")) - name collision between that target's own slug and this target's slug ($SLUG). Two units cannot share one filename; rename one of the two target directories to resolve it." >&2
  FAILED=1
  return 1
}

# Delete $1 (after running $2, if given - e.g. 'launchctl unload' or
# 'systemctl --user disable --now') only if `ours`($1) - otherwise leave it
# alone with a loud warning. Never counts as a failure: declining to touch
# someone else's unit is correct behavior, not an error in THIS target's install.
delete_guarded() {  # delete_guarded <path> [stop-cmd...]
  local path="$1"; shift
  [ -e "$path" ] || return 1
  if ours "$path"; then
    if [ $# -gt 0 ]; then "$@" "$path" 2>/dev/null || true; fi
    rm -f "$path"
    return 0
  fi
  echo "! leaving $path alone: it belongs to a DIFFERENT target ($(unit_owner "$path")) - name collision with this target's slug ($SLUG), not this target's own unit" >&2
  return 1
}

# CANDIDATE slot numbers: any file in <dir> matching this slug's naming
# pattern, regardless of ownership. Ownership is verified separately, per
# file, immediately before any mutation (write_guarded/delete_guarded above) -
# this only narrows WHICH filenames are even in play.
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
    plist_ok=0; service_ok=0
    write_guarded "$GEN_DIR/$label.plist" gen_plist "$n" && plist_ok=1
    write_guarded "$GEN_DIR/$unit" gen_systemd "$n" && service_ok=1
    [ "$plist_ok" -eq 1 ] && [ "$service_ok" -eq 1 ] && echo "✓ generated $label.plist + $unit (slot $n/$SLOTS)"
    n=$((n + 1))
  done
  for n in $(existing_slots "$GEN_DIR"); do
    if [ "$n" -ge "$SLOTS" ]; then
      removed=0
      delete_guarded "$GEN_DIR/$(label_for "$n").plist" && removed=1
      delete_guarded "$GEN_DIR/$(unit_for "$n")" && removed=1
      [ "$removed" -eq 1 ] && echo "✓ removed stale slot $n - concurrency is now $SLOTS"
    fi
  done
  exit "$FAILED"
fi

case "$(uname -s)" in
  Darwin)
    UNIT_DIR="$HOME/Library/LaunchAgents"; mkdir -p "$UNIT_DIR"
    if [ "$MODE" = "uninstall" ]; then
      for n in $(existing_slots "$UNIT_DIR"); do
        label="$(label_for "$n")"; plist="$UNIT_DIR/$label.plist"
        delete_guarded "$plist" launchctl unload && echo "✓ removed $label"
      done
      exit 0
    fi
    n=0
    while [ "$n" -lt "$SLOTS" ]; do
      label="$(label_for "$n")"; plist="$UNIT_DIR/$label.plist"
      if write_guarded "$plist" gen_plist "$n"; then
        launchctl unload "$plist" 2>/dev/null || true
        launchctl load "$plist"
        echo "✓ launchd agent $label loaded (slot $n/$SLOTS, RunAtLoad + KeepAlive). Logs: $LOG"
      fi
      n=$((n + 1))
    done
    for n in $(existing_slots "$UNIT_DIR"); do
      if [ "$n" -ge "$SLOTS" ]; then
        label="$(label_for "$n")"; plist="$UNIT_DIR/$label.plist"
        delete_guarded "$plist" launchctl unload && echo "✓ removed stale slot $n ($label) - concurrency is now $SLOTS"
      fi
    done
    echo "  stop:  $ORBIT_HOME/install/install_service.sh $TARGET $ORBIT_HOME --uninstall"
    ;;
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
    if [ "$MODE" = "uninstall" ]; then
      for n in $(existing_slots "$UNIT_DIR"); do
        unit="$(unit_for "$n")"
        if ours "$UNIT_DIR/$unit"; then
          systemctl --user disable --now "$unit" 2>/dev/null || true
          rm -f "$UNIT_DIR/$unit"
          echo "✓ removed $unit"
        else
          echo "! leaving $UNIT_DIR/$unit alone: it belongs to a DIFFERENT target ($(unit_owner "$UNIT_DIR/$unit")) - name collision with this target's slug ($SLUG), not this target's own unit" >&2
        fi
      done
      systemctl --user daemon-reload 2>/dev/null || true
      exit 0
    fi
    n=0
    while [ "$n" -lt "$SLOTS" ]; do
      write_guarded "$UNIT_DIR/$(unit_for "$n")" gen_systemd "$n"
      n=$((n + 1))
    done
    for n in $(existing_slots "$UNIT_DIR"); do
      if [ "$n" -ge "$SLOTS" ]; then
        unit="$(unit_for "$n")"
        if ours "$UNIT_DIR/$unit"; then
          systemctl --user disable --now "$unit" 2>/dev/null || true
          rm -f "$UNIT_DIR/$unit"
          echo "✓ removed stale slot $n ($unit) - concurrency is now $SLOTS"
        else
          echo "! leaving $UNIT_DIR/$unit alone: it belongs to a DIFFERENT target ($(unit_owner "$UNIT_DIR/$unit")) - name collision with this target's slug ($SLUG), not this target's own unit" >&2
        fi
      fi
    done
    systemctl --user daemon-reload
    n=0
    while [ "$n" -lt "$SLOTS" ]; do
      unit="$(unit_for "$n")"
      [ -e "$UNIT_DIR/$unit" ] && ours "$UNIT_DIR/$unit" && systemctl --user enable --now "$unit"
      n=$((n + 1))
    done
    echo "✓ systemd --user units enabled + started for $SLOTS slot(s). Logs: journalctl --user -u $SLUG -f"
    echo "  stop:  $ORBIT_HOME/install/install_service.sh $TARGET $ORBIT_HOME --uninstall"
    ;;
  *) echo "unsupported OS $(uname -s) - run each slot manually: ORBIT_SLOT=<n> ORBIT_SLOTS=$SLOTS ORBIT_HOME=$ORBIT_HOME $RUN $TARGET"; exit 1 ;;
esac
exit "$FAILED"
