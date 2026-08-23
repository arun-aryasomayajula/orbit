# Task 7 (M5): multi-slot service units. install_service.sh now writes one
# launchd/systemd unit PER concurrency slot instead of a single slot-0-only
# unit, with ORBIT_SLOT/ORBIT_SLOTS baked into each. Slot 0 keeps the LEGACY
# label/unit name (`com.orbit.orbit-<slug>` / `orbit-<slug>.service`) so an
# existing single-slot install upgrades in place; slots 1..N-1 get a
# `-slotN` suffix. Re-running reconciles: adds units for slots that gained a
# unit, removes units for slots concurrency no longer wants.
#
# `--generate-only <dir>` is the test seam: it writes both the plist AND the
# systemd unit for every slot (host-independent — no launchctl/systemctl
# involved) so these tests exercise real content on any platform. The real
# per-OS install/uninstall paths (launchctl/systemctl calls) are covered by
# `bash -n` + manual review only — see task-7-report.md.
#
# doctor.py's service-slot-count advisory is covered separately below via
# fake `launchctl`/`systemctl` executables on PATH (same pattern as
# tests/test_qa_config.py's fake node/npx).
#
# Run: python3.11 -m pytest tests/test_service_units.py -v   (from orbit/)
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE = REPO_ROOT / "engine"
INSTALL_SERVICE = REPO_ROOT / "install" / "install_service.sh"
DOCTOR = ENGINE / "doctor.py"


def _target(tmp_path, concurrency=None, name="myrepo"):
    t = tmp_path / name
    ap = t / ".autopilot"
    ap.mkdir(parents=True)
    cfg = 'repo: "."\nbase_branch: "main"\ngates:\n  test: {cmd: "true", cwd: "."}\n'
    if concurrency is not None:
        cfg += f"concurrency: {concurrency}\n"
    (ap / "config.yaml").write_text(cfg)
    return t


def _slug(target_name):
    """install_service.sh's slug: 'orbit-' + basename with every non-alnum
    char replaced by a dash, trailing dashes stripped."""
    return "orbit-" + re.sub(r"[^a-zA-Z0-9]", "-", target_name).rstrip("-")


def _generate(target, gen_dir, env=None):
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run(
        ["bash", str(INSTALL_SERVICE), str(target), str(REPO_ROOT), "--generate-only", str(gen_dir)],
        capture_output=True, text=True, env=e,
    )
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    return r


# === install_service.sh --generate-only: content ============================

def test_generate_only_writes_legacy_slot0_name_and_env(tmp_path):
    t = _target(tmp_path, concurrency=1, name="myrepo")
    gen = tmp_path / "gen"
    _generate(t, gen)
    slug = _slug("myrepo")

    plist = (gen / f"com.orbit.{slug}.plist").read_text()
    assert f"<string>com.orbit.{slug}</string>" in plist
    assert "<key>ORBIT_SLOT</key><string>0</string>" in plist
    assert "<key>ORBIT_SLOTS</key><string>1</string>" in plist
    assert f"<string>{t}</string>" in plist
    assert f"<string>{REPO_ROOT}</string>" in plist

    unit = (gen / f"{slug}.service").read_text()
    assert "Environment=ORBIT_SLOT=0" in unit
    assert "Environment=ORBIT_SLOTS=1" in unit
    assert f"ExecStart=/bin/bash {REPO_ROOT}/engine/run.sh {t}" in unit


def test_generate_only_writes_one_unit_pair_per_slot_with_slot_suffix(tmp_path):
    t = _target(tmp_path, concurrency=3, name="myrepo")
    gen = tmp_path / "gen"
    _generate(t, gen)
    slug = _slug("myrepo")

    expected = {f"com.orbit.{slug}.plist", f"com.orbit.{slug}-slot1.plist", f"com.orbit.{slug}-slot2.plist",
                f"{slug}.service", f"{slug}-slot1.service", f"{slug}-slot2.service"}
    assert {p.name for p in gen.iterdir()} == expected

    for n in (1, 2):
        plist = (gen / f"com.orbit.{slug}-slot{n}.plist").read_text()
        assert f"<string>com.orbit.{slug}-slot{n}</string>" in plist
        assert f"<key>ORBIT_SLOT</key><string>{n}</string>" in plist
        assert "<key>ORBIT_SLOTS</key><string>3</string>" in plist

        unit = (gen / f"{slug}-slot{n}.service").read_text()
        assert f"Environment=ORBIT_SLOT={n}" in unit
        assert "Environment=ORBIT_SLOTS=3" in unit


def test_generate_only_reconcile_removes_stale_slots_on_lower_concurrency(tmp_path):
    t = _target(tmp_path, concurrency=3, name="myrepo")
    gen = tmp_path / "gen"
    _generate(t, gen)
    slug = _slug("myrepo")
    assert (gen / f"com.orbit.{slug}-slot2.plist").exists()

    # Lower concurrency, regenerate into the SAME dir.
    (t / ".autopilot" / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates:\n  test: {cmd: "true", cwd: "."}\nconcurrency: 1\n')
    _generate(t, gen)

    remaining = {p.name for p in gen.iterdir()}
    assert remaining == {f"com.orbit.{slug}.plist", f"{slug}.service"}
    # Slot 0's unit is still the legacy name, untouched by the drop.
    assert f"<key>ORBIT_SLOTS</key><string>1</string>" in (gen / f"com.orbit.{slug}.plist").read_text()


def test_generate_only_reconcile_adds_missing_slots_on_higher_concurrency(tmp_path):
    t = _target(tmp_path, concurrency=1, name="myrepo")
    gen = tmp_path / "gen"
    _generate(t, gen)
    slug = _slug("myrepo")
    assert {p.name for p in gen.iterdir()} == {f"com.orbit.{slug}.plist", f"{slug}.service"}

    (t / ".autopilot" / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates:\n  test: {cmd: "true", cwd: "."}\nconcurrency: 3\n')
    _generate(t, gen)

    expected = {f"com.orbit.{slug}.plist", f"com.orbit.{slug}-slot1.plist", f"com.orbit.{slug}-slot2.plist",
                f"{slug}.service", f"{slug}-slot1.service", f"{slug}-slot2.service"}
    assert {p.name for p in gen.iterdir()} == expected


def test_generate_only_reconcile_is_scoped_to_this_targets_slug(tmp_path):
    """Reconcile must never remove another target's units, or any non-orbit
    file, sitting in the same directory."""
    t = _target(tmp_path, concurrency=1, name="myrepo")
    gen = tmp_path / "gen"
    gen.mkdir()
    other = gen / "com.orbit.orbit-otherrepo.plist"
    other.write_text("unrelated")
    non_orbit = gen / "com.apple.something.plist"
    non_orbit.write_text("unrelated")

    _generate(t, gen)

    assert other.exists()
    assert non_orbit.exists()


def _generate_allow_fail(target, gen_dir):
    """Like _generate, but for scenarios expected to (partially) fail -
    a collision refusal exits nonzero even though the other slots still
    got written."""
    return subprocess.run(
        ["bash", str(INSTALL_SERVICE), str(target), str(REPO_ROOT), "--generate-only", str(gen_dir)],
        capture_output=True, text=True,
    )


def test_generate_only_refuses_to_overwrite_a_different_targets_colliding_unit(tmp_path):
    """Regression: target `app`'s derived slot-1 filename
    (`orbit-app-slot1.*`) is byte-identical to a DIFFERENT target dir named
    literally `app-slot1`'s own legacy slot-0 filename. install_service.sh
    must never let one target's install clobber the other's real unit just
    because the names collide - it must refuse that slot's write instead.
    """
    app_slot1 = _target(tmp_path, concurrency=1, name="app-slot1")
    app = _target(tmp_path, concurrency=3, name="app")
    gen = tmp_path / "gen"

    # app-slot1 installs FIRST, genuinely owning the shared filename as its
    # own (legacy) slot 0.
    r1 = _generate_allow_fail(app_slot1, gen)
    assert r1.returncode == 0, r1.stderr
    colliding_plist = gen / "com.orbit.orbit-app-slot1.plist"
    colliding_service = gen / "orbit-app-slot1.service"
    assert f"<string>{app_slot1}</string>" in colliding_plist.read_text()

    # app installs SECOND. Its own slot 0 (orbit-app.*) and slot 2
    # (orbit-app-slot2.*) are unique and must succeed; its slot 1
    # (orbit-app-slot1.*) collides with app-slot1's real unit and must be
    # REFUSED, not overwritten - the script must exit nonzero to signal it.
    r2 = _generate_allow_fail(app, gen)
    assert r2.returncode != 0, r2.stdout
    assert "belongs to a DIFFERENT target" in r2.stderr
    assert str(app_slot1) in r2.stderr

    assert (gen / "com.orbit.orbit-app.plist").exists()
    assert (gen / "com.orbit.orbit-app-slot2.plist").exists()
    # The colliding unit is UNCHANGED - still app-slot1's, not app's.
    assert f"<string>{app_slot1}</string>" in colliding_plist.read_text()
    assert f"ORBIT_TARGET={app_slot1}" in colliding_service.read_text()
    assert f"<string>{app}</string>" not in colliding_plist.read_text()


def test_generate_only_reconcile_down_never_deletes_a_different_targets_unit(tmp_path):
    """Continuation of the collision above: lowering `app`'s concurrency
    must reconcile away app's OWN stale slots, but must never delete
    app-slot1's real unit just because its filename matches app's naming
    pattern for a slot app no longer wants."""
    app_slot1 = _target(tmp_path, concurrency=1, name="app-slot1")
    app = _target(tmp_path, concurrency=3, name="app")
    gen = tmp_path / "gen"
    assert _generate_allow_fail(app_slot1, gen).returncode == 0
    assert _generate_allow_fail(app, gen).returncode != 0  # slot 1 refused, as above

    # Lower app's concurrency: app's slot 1 and slot 2 both become "stale"
    # from app's point of view. Slot 2 is genuinely app's own -> removed.
    # Slot 1's file is genuinely app-slot1's -> must survive, untouched.
    (app / ".autopilot" / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates:\n  test: {cmd: "true", cwd: "."}\nconcurrency: 1\n')
    r3 = _generate_allow_fail(app, gen)
    assert r3.returncode == 0, r3.stdout + r3.stderr
    assert "leaving" in r3.stderr and str(app_slot1) in r3.stderr

    colliding_plist = gen / "com.orbit.orbit-app-slot1.plist"
    colliding_service = gen / "orbit-app-slot1.service"
    assert colliding_plist.exists() and colliding_service.exists()
    assert f"<string>{app_slot1}</string>" in colliding_plist.read_text()
    assert not (gen / "com.orbit.orbit-app-slot2.plist").exists()
    assert not (gen / "orbit-app-slot2.service").exists()
    assert (gen / "com.orbit.orbit-app.plist").exists()  # app's own slot 0 survives


def test_install_service_script_is_syntactically_valid():
    r = subprocess.run(["bash", "-n", str(INSTALL_SERVICE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_generate_only_rejects_missing_directory_argument(tmp_path):
    t = _target(tmp_path, concurrency=1, name="myrepo")
    r = subprocess.run(["bash", str(INSTALL_SERVICE), str(t), str(REPO_ROOT), "--generate-only"],
                       capture_output=True, text=True)
    assert r.returncode != 0


# === doctor.py — installed service slot count advisory ======================

def _fake_service_bin(tmp_path, slug, installed_slots):
    """A tmp bindir with launchctl/systemctl stubs reporting units installed
    for `slug` at the given slot numbers, in each tool's real output shape.
    Whichever platform actually runs the test only invokes the matching
    stub; the other is simply never called."""
    bindir = tmp_path / "svcbin"
    bindir.mkdir()

    launchd_lines = ["PID\tStatus\tLabel", "-\t0\tcom.orbit.some-unrelated-service"]
    systemd_lines = ["unrelated.service loaded active running Something else"]
    for n in installed_slots:
        label = f"com.orbit.{slug}" if n == 0 else f"com.orbit.{slug}-slot{n}"
        unit = f"{slug}.service" if n == 0 else f"{slug}-slot{n}.service"
        launchd_lines.append(f"-\t0\t{label}")
        systemd_lines.append(f"{unit} loaded active running Orbit loop")

    launchctl = bindir / "launchctl"
    launchctl.write_text("#!/bin/sh\nif [ \"$1\" = list ]; then cat <<'EOF'\n" + "\n".join(launchd_lines) + "\nEOF\nfi\n")
    systemctl = bindir / "systemctl"
    systemctl.write_text("#!/bin/sh\nif [ \"$1\" = --user ]; then cat <<'EOF'\n" + "\n".join(systemd_lines) + "\nEOF\nfi\n")
    for f in (launchctl, systemctl):
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def _run_doctor(target, path_prefix=None):
    e = dict(os.environ)
    if path_prefix is not None:
        e["PATH"] = str(path_prefix) + os.pathsep + e.get("PATH", "")
    return subprocess.run([sys.executable, str(DOCTOR), str(target)], capture_output=True, text=True, env=e)


def _advisory_lines(stdout):
    return [ln.strip() for ln in stdout.splitlines() if ln.strip().startswith("·")]


def test_doctor_service_advisory_when_installed_count_differs_from_concurrency(tmp_path):
    t = _target(tmp_path, concurrency=3, name="myrepo")
    slug = _slug("myrepo")
    bindir = _fake_service_bin(tmp_path, slug, installed_slots=[0])  # only 1 installed, concurrency wants 3
    r = _run_doctor(t, path_prefix=bindir)
    lines = _advisory_lines(r.stdout)
    assert any("service:" in ln and "1 installed" in ln and "concurrency wants 3" in ln for ln in lines), r.stdout


def test_doctor_service_no_advisory_when_counts_match(tmp_path):
    t = _target(tmp_path, concurrency=2, name="myrepo")
    slug = _slug("myrepo")
    bindir = _fake_service_bin(tmp_path, slug, installed_slots=[0, 1])
    r = _run_doctor(t, path_prefix=bindir)
    lines = _advisory_lines(r.stdout)
    assert not any("service:" in ln for ln in lines), r.stdout
    assert "ALL CHECKS PASSED" in r.stdout


def test_doctor_service_skips_silently_when_no_service_installed(tmp_path):
    t = _target(tmp_path, concurrency=3, name="myrepo")
    slug = _slug("myrepo")
    bindir = _fake_service_bin(tmp_path, slug, installed_slots=[])  # nothing installed for this slug
    r = _run_doctor(t, path_prefix=bindir)
    lines = _advisory_lines(r.stdout)
    assert not any("service:" in ln for ln in lines), r.stdout
    assert "ALL CHECKS PASSED" in r.stdout


def test_doctor_service_excludes_a_label_owned_by_a_different_target_on_disk(tmp_path):
    """Regression: launchctl/systemctl can report a label/unit name that
    matches THIS target's slug by prefix but is actually installed for a
    DIFFERENT target whose own slug collides with one of ours (the same
    `<slug>-slot<N>` collision install_service.sh guards against). doctor
    must verify ORBIT_TARGET on disk and NOT count that label as ours - a
    label doctor can't verify at all (no on-disk file) still falls back to
    the prefix match, so this isn't a blanket "trust nothing" regression.
    """
    t = _target(tmp_path, concurrency=3, name="app")
    slug = _slug("app")  # "orbit-app"

    # Fake HOME so ~/Library/LaunchAgents and ~/.config/systemd/user are
    # redirected into a tmp dir - os.path.expanduser("~...") honors $HOME.
    fake_home = tmp_path / "fakehome"
    la_dir = fake_home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    sysd_dir = fake_home / ".config" / "systemd" / "user"
    sysd_dir.mkdir(parents=True)

    # slot 1's label/unit is genuinely installed for a DIFFERENT target -
    # its on-disk file says so.
    (la_dir / f"com.orbit.{slug}-slot1.plist").write_text(
        "<?xml version=\"1.0\"?><plist><dict>"
        "<key>EnvironmentVariables</key><dict>"
        "<key>ORBIT_TARGET</key><string>/somewhere/else/app-slot1</string>"
        "</dict></dict></plist>")
    (sysd_dir / f"{slug}-slot1.service").write_text("Environment=ORBIT_TARGET=/somewhere/else/app-slot1\n")
    # slots 0 and 2 have no on-disk file doctor can check - best effort keeps
    # trusting the launchctl/systemctl prefix match for those.

    bindir = _fake_service_bin(tmp_path, slug, installed_slots=[0, 1, 2])
    e = dict(os.environ)
    e["PATH"] = str(bindir) + os.pathsep + e.get("PATH", "")
    e["HOME"] = str(fake_home)
    r = subprocess.run([sys.executable, str(DOCTOR), str(t)], capture_output=True, text=True, env=e)

    lines = _advisory_lines(r.stdout)
    # Naively, 3 labels match by prefix == concurrency 3 -> silent. With the
    # ownership check, slot 1 is excluded (foreign) -> only 2 count -> advisory.
    assert any("service:" in ln and "2 installed" in ln and "concurrency wants 3" in ln for ln in lines), r.stdout


def test_doctor_service_advisory_never_fails_doctor(tmp_path):
    """The advisory must never flip doctor's exit code or ALL CHECKS PASSED,
    even on a mismatch — it's informational only."""
    t = _target(tmp_path, concurrency=5, name="myrepo")
    slug = _slug("myrepo")
    bindir = _fake_service_bin(tmp_path, slug, installed_slots=[0])
    r = _run_doctor(t, path_prefix=bindir)
    assert r.returncode == 0, r.stdout
    assert "ALL CHECKS PASSED" in r.stdout


# === install_service.sh - Linux install path continues past a collision ====
# `--generate-only` never exercised this: it calls write_guarded from an `&&`
# chain, which `set -e` doesn't trap. The REAL Linux install loop used to call
# write_guarded as a bare statement, so `set -e` aborted the whole script the
# moment ONE slot collided with a different target's on-disk unit - never
# reaching daemon-reload, the stale-slot reconcile-down loop, or later slots
# in the very same loop. Exercised here with a faked `uname`/`systemctl` on
# PATH so it runs on any host, same spirit as doctor's fake launchctl/systemctl.

def _fake_uname_and_systemctl(tmp_path, calls_log):
    bindir = tmp_path / "linux-bin"
    bindir.mkdir()
    (bindir / "uname").write_text("#!/bin/sh\necho Linux\n")
    (bindir / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {calls_log}\nexit 0\n')
    for f in (bindir / "uname", bindir / "systemctl"):
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def test_linux_install_continues_past_a_collision_and_reports_failure(tmp_path):
    fake_home = tmp_path / "fakehome"
    sysd_dir = fake_home / ".config" / "systemd" / "user"
    sysd_dir.mkdir(parents=True)

    # A unit belonging to a DIFFERENT target, whose filename collides with
    # what THIS target's slot 1 wants to write (the exact "<slug>-slotN"
    # collision install_service.sh's header describes).
    other_target = tmp_path / "app-slot1"
    other_target.mkdir()
    (sysd_dir / "orbit-app-slot1.service").write_text(
        f"[Service]\nEnvironment=ORBIT_TARGET={other_target}\n")

    calls_log = tmp_path / "systemctl-calls.log"
    bindir = _fake_uname_and_systemctl(tmp_path, calls_log)

    t = _target(tmp_path, concurrency=2, name="app")
    env = dict(os.environ, HOME=str(fake_home),
              PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    r = subprocess.run(["bash", str(INSTALL_SERVICE), str(t), str(REPO_ROOT)],
                       capture_output=True, text=True, env=env)

    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "refusing" in r.stderr and "DIFFERENT target" in r.stderr
    # slot 0's unique filename was still installed despite slot 1's collision
    assert (sysd_dir / "orbit-app.service").exists()
    # the other target's file was left completely alone
    assert f"ORBIT_TARGET={other_target}" in (sysd_dir / "orbit-app-slot1.service").read_text()
    # the script kept going past the collision: daemon-reload ran, and the
    # final success line for the slots that DID install still printed
    assert "daemon-reload" in calls_log.read_text()
    assert "systemd --user units enabled" in r.stdout


def test_shellenv_failure_aborts_the_install_before_any_unit_is_touched(tmp_path):
    """Regression: `eval "$(cmd)"` on its own swallows `cmd`'s own exit
    status - eval succeeds trivially on the empty string a failed, silent
    config.py leaves behind, so the `|| exit 2` guard never fired even
    though config never loaded. A broken config.yaml must abort install
    before anything is written."""
    fake_home = tmp_path / "fakehome"
    (fake_home / ".config" / "systemd" / "user").mkdir(parents=True)
    calls_log = tmp_path / "systemctl-calls.log"
    bindir = _fake_uname_and_systemctl(tmp_path, calls_log)

    t = _target(tmp_path, concurrency=2, name="app")
    (t / ".autopilot" / "config.yaml").write_text("not: valid: yaml: [")

    env = dict(os.environ, HOME=str(fake_home),
              PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    r = subprocess.run(["bash", str(INSTALL_SERVICE), str(t), str(REPO_ROOT)],
                       capture_output=True, text=True, env=env)

    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "config load failed" in r.stdout + r.stderr
    assert not list((fake_home / ".config" / "systemd" / "user").glob("*.service"))
