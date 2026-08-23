# Task (M5 fix wave, Important 3): "Restart loop" used to restart only slot
# 0's legacy launchd label (com.orbit.orbit-<slug>) even though `concurrency`
# > 1 installs one unit PER SLOT (Task 7). Two bugs, tested here:
#   - loop_loaded() substring-matched LAUNCHD_LABEL against the WHOLE
#     `launchctl list` output, so a slot-1-only load (whose label is
#     LAUNCHD_LABEL + "-slot1", i.e. LAUNCHD_LABEL as a PREFIX) counted as
#     "the loop is loaded" even when slot 0 itself never came up.
#   - do_restart(LAUNCHD_LABEL) only ever kicked ONE label - do_restart_
#     loop_all replaces it, enumerating every unit this target actually owns
#     on disk (verified via ORBIT_TARGET, same ownership check install_
#     service.sh/doctor.py use) and restarting each one.
#
# Run: python3.11 -m pytest tests/test_command_center_restart.py -v   (from orbit/)
import importlib.util
import stat
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap = tmp_path / "myrepo" / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks:\n")
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    return _mod("command_center")


def _write_plist(path, target):
    path.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\"><dict>\n"
        "<key>EnvironmentVariables</key><dict>\n"
        f"<key>ORBIT_TARGET</key><string>{target}</string>\n"
        "</dict>\n"
        "</dict></plist>\n")


# ── loop_loaded(): exact label match, not a substring ──────────────────────

def test_loop_loaded_true_when_slot_0_label_is_an_exact_line_match(cc, monkeypatch):
    monkeypatch.setattr(cc, "LAUNCHD_LABEL", "com.orbit.orbit-app")

    class R:
        stdout = "12345\t0\tcom.orbit.orbit-app\n"

    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: R())
    assert cc.loop_loaded() is True


def test_loop_loaded_false_when_only_a_slot_1_sibling_is_loaded(cc, monkeypatch):
    """Regression: LAUNCHD_LABEL is a PREFIX of the slot-1 label - a bare
    substring test would wrongly say slot 0 is loaded here too."""
    monkeypatch.setattr(cc, "LAUNCHD_LABEL", "com.orbit.orbit-app")

    class R:
        stdout = "12345\t0\tcom.orbit.orbit-app-slot1\n"

    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: R())
    assert cc.loop_loaded() is False


def test_loop_loaded_false_when_nothing_matches(cc, monkeypatch):
    monkeypatch.setattr(cc, "LAUNCHD_LABEL", "com.orbit.orbit-app")

    class R:
        stdout = "12345\t0\tcom.orbit.something-else\n"

    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: R())
    assert cc.loop_loaded() is False


# ── _installed_target_units(): ownership-verified enumeration ─────────────

def test_installed_target_units_excludes_a_foreign_owned_collision_file(cc, monkeypatch, tmp_path):
    """Regression: a DIFFERENT target's slug can produce a byte-identical
    `-slotN` unit filename to one of THIS target's own units (the exact
    collision install_service.sh/doctor.py guard against). A label that
    matches by NAME but whose on-disk file's ORBIT_TARGET names a different
    target must never be counted as this target's own."""
    fake_home = tmp_path / "fakehome"
    la_dir = fake_home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    slug = cc._service_slug()
    _write_plist(la_dir / f"com.orbit.{slug}.plist", str(cc.REPO))
    other_target = tmp_path / "some-other-target"
    _write_plist(la_dir / f"com.orbit.{slug}-slot1.plist", str(other_target))

    units = cc._installed_target_units(is_darwin=True)
    assert units == [f"com.orbit.{slug}"]


def test_installed_target_units_includes_every_owned_slot(cc, monkeypatch, tmp_path):
    fake_home = tmp_path / "fakehome"
    la_dir = fake_home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    slug = cc._service_slug()
    _write_plist(la_dir / f"com.orbit.{slug}.plist", str(cc.REPO))
    _write_plist(la_dir / f"com.orbit.{slug}-slot1.plist", str(cc.REPO))
    _write_plist(la_dir / f"com.orbit.{slug}-slot2.plist", str(cc.REPO))

    units = cc._installed_target_units(is_darwin=True)
    assert sorted(units) == sorted(
        [f"com.orbit.{slug}", f"com.orbit.{slug}-slot1", f"com.orbit.{slug}-slot2"])


def test_installed_target_units_empty_when_nothing_installed(cc, monkeypatch, tmp_path):
    fake_home = tmp_path / "fakehome"
    monkeypatch.setenv("HOME", str(fake_home))
    assert cc._installed_target_units(is_darwin=True) == []


# ── do_restart_loop_all(): restarts every owned unit, reports the count ────

def test_do_restart_loop_all_restarts_every_owned_slot(cc, monkeypatch, tmp_path):
    fake_home = tmp_path / "fakehome"
    la_dir = fake_home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    slug = cc._service_slug()
    labels = [f"com.orbit.{slug}", f"com.orbit.{slug}-slot1", f"com.orbit.{slug}-slot2"]
    for label in labels:
        _write_plist(la_dir / f"{label}.plist", str(cc.REPO))
    # a foreign-owned collision must never be kicked
    _write_plist(la_dir / f"com.orbit.{slug}-slot3.plist", str(tmp_path / "other"))

    kicked = []

    class R:
        returncode = 0
        stderr = stdout = ""

    def fake_run(argv, **kw):
        kicked.append(argv)
        return R()

    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    msg = cc.do_restart_loop_all(is_darwin=True)

    assert len(kicked) == 3
    kicked_labels = {argv[-1].split("/")[-1] for argv in kicked}
    assert kicked_labels == set(labels)
    assert "Restarted 3 unit(s)" in msg
    for label in labels:
        assert label in msg


def test_do_restart_loop_all_reports_partial_failure(cc, monkeypatch, tmp_path):
    fake_home = tmp_path / "fakehome"
    la_dir = fake_home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    slug = cc._service_slug()
    _write_plist(la_dir / f"com.orbit.{slug}.plist", str(cc.REPO))
    _write_plist(la_dir / f"com.orbit.{slug}-slot1.plist", str(cc.REPO))

    class Ok:
        returncode = 0
        stderr = stdout = ""

    class Fail:
        returncode = 1
        stderr = "no such service"
        stdout = ""

    def fake_run(argv, **kw):
        return Fail() if argv[-1].endswith("-slot1") else Ok()

    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    msg = cc.do_restart_loop_all(is_darwin=True)

    assert "Restarted 1/2 unit(s)" in msg
    assert "no such service" in msg


def test_do_restart_loop_all_reports_nothing_installed(cc, monkeypatch, tmp_path):
    fake_home = tmp_path / "fakehome"
    monkeypatch.setenv("HOME", str(fake_home))
    msg = cc.do_restart_loop_all(is_darwin=True)
    assert "No installed service units" in msg


def test_do_restart_loop_all_never_kicks_a_foreign_owned_unit_alone(cc, monkeypatch, tmp_path):
    """Only a foreign-owned collision file exists for this target's slug -
    nothing installed for real, so nothing should be restarted."""
    fake_home = tmp_path / "fakehome"
    la_dir = fake_home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    slug = cc._service_slug()
    _write_plist(la_dir / f"com.orbit.{slug}.plist", str(tmp_path / "other-target"))

    kicked = []
    monkeypatch.setattr(cc.subprocess, "run", lambda argv, **kw: kicked.append(argv))
    msg = cc.do_restart_loop_all(is_darwin=True)

    assert not kicked
    assert "No installed service units" in msg
