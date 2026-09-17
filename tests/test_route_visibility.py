# Chunk 4: the dashboard's route payload — planned (live router resolution),
# actual (ledger route record), mismatch flag, and the category→route table
# with trust tiers. Tests target the pure helpers, not full build_state
# (which shells out to git). Run: .venv/bin/pytest tests/test_route_visibility.py -v

import importlib
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"

ROUTER_YAML = """\
goldens:
  by_category:
    feature: feature
categories:
  feature:
    policy: allow
    maker: {agent: builder, model: opus, effort: high}
    skill: ccos-route
  security:
    policy: human
    maker: {agent: builder, model: opus, effort: high}
    skill: security-review
"""


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    ap_state = ap_home / "state"
    ap_state.mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_state))
    (ap_home / "backlog.yaml").write_text("version: 1\ntasks: []\n")
    (ap_home / "router.yaml").write_text(ROUTER_YAML)
    monkeypatch.syspath_prepend(str(ENGINE))
    import route_resolve
    import command_center
    importlib.reload(route_resolve)
    return importlib.reload(command_center)


def _router(cc_mod):
    return cc_mod.route_resolve.load_router(cc_mod.AP_HOME)


def test_router_state_source_tiers_and_rows(cc):
    rs = cc.router_state(_router(cc))
    assert rs["source"] == "override"
    rows = {r["category"]: r for r in rs["categories"]}
    # Routed override category, core tier:
    assert rows["feature"]["tier"] == "core"
    assert rows["feature"]["skill"] == "ccos-route"
    assert rows["feature"]["golden"] == "feature"
    assert rows["feature"]["routed"] is True
    # Human-only tier:
    assert rows["security"]["tier"] == "human-only"
    # KNOWN_CATEGORIES not in this override still appear, unrouted:
    assert rows["epic"]["tier"] == "planning"
    assert rows["epic"]["routed"] is False
    assert rows["testing"]["tier"] == "safe"


def test_router_state_none_when_no_router(cc):
    assert cc.router_state(None) is None


def test_route_fields_planned_only(cc):
    f = cc._route_fields("feature", None, _router(cc))
    assert f["route_planned"]["skill"] == "ccos-route"
    assert f["route_actual"] is None
    assert "route_mismatch" not in f


def test_route_fields_actual_match_no_flag(cc):
    router = _router(cc)
    entry = {"route": dict(cc.route_resolve.resolve_route("feature", router))}
    f = cc._route_fields("feature", entry, router)
    assert f["route_actual"]["skill"] == "ccos-route"
    assert "route_mismatch" not in f


def test_route_fields_mismatch_flagged(cc):
    router = _router(cc)
    old = dict(cc.route_resolve.resolve_route("feature", router))
    old["skill"] = "superpowers:writing-plans"   # router changed since the run
    f = cc._route_fields("feature", {"route": old}, router)
    assert f["route_mismatch"] is True


def test_route_fields_unknown_category(cc):
    f = cc._route_fields("nonexistent", None, _router(cc))
    assert f["route_planned"] is None
    assert f["route_actual"] is None


def test_route_fields_tolerates_corrupt_actual(cc):
    # A hand-edited ledger with a non-dict route must not crash build_state.
    f = cc._route_fields("feature", {"route": "corrupt-string"}, _router(cc))
    assert f["route_actual"] is None
    assert "route_mismatch" not in f
