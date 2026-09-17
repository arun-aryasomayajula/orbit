# Chunk 4 (portal route visibility): the shared category→route resolver, the
# ledger `route` verb, and the claim-time recorder. One resolution rule for
# recorder and dashboard: target .autopilot/router.yaml wins, else the engine
# default. Run: .venv/bin/pytest tests/test_route_record.py -v   (from orbit/)

import importlib
import json
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
    tracks: [frontend-react]
  bug:
    policy: allow
    maker: {agent: builder, model: opus, effort: high}
    skill: superpowers:systematic-debugging
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Temp AP_HOME/AP_STATE with an override router + a one-task queue.
    Reloads the three engine modules against the temp env (module-level
    constants like ledger.LEDGER bind at import). Reload order matters:
    dependencies first, route_record last."""
    ap_home = tmp_path / ".autopilot"
    ap_state = ap_home / "state"
    ap_state.mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_state))
    monkeypatch.syspath_prepend(str(ENGINE))
    (ap_home / "router.yaml").write_text(ROUTER_YAML)
    (ap_state / "queue.json").write_text(json.dumps(
        {"tasks": [{"id": "t1", "category": "feature", "task": "t1 title"}]}))
    import ledger
    import route_resolve
    import route_record
    ledger = importlib.reload(ledger)
    route_resolve = importlib.reload(route_resolve)
    route_record = importlib.reload(route_record)
    return {"ap_home": ap_home, "ap_state": ap_state, "ledger": ledger,
            "route_resolve": route_resolve, "route_record": route_record}


def test_override_wins_and_normalizes(env):
    rr = env["route_resolve"]
    router = rr.load_router(env["ap_home"])
    assert router["source"] == "override"
    assert router["path"] == str(env["ap_home"] / "router.yaml")
    r = rr.resolve_route("feature", router)
    assert r == {"category": "feature", "maker": "builder", "model": "opus",
                 "effort": "high", "skill": "ccos-route",
                 "tracks": ["frontend-react"], "policy": "allow",
                 "golden": "feature"}


def test_falls_back_to_engine_default(env, tmp_path):
    rr = env["route_resolve"]
    bare = tmp_path / "no-override"
    bare.mkdir()
    router = rr.load_router(bare)
    assert router["source"] == "default"
    # The real engine default routes bug via systematic-debugging.
    assert rr.resolve_route("bug", router)["skill"] == "superpowers:systematic-debugging"


def test_unknown_category_resolves_to_none(env):
    rr = env["route_resolve"]
    router = rr.load_router(env["ap_home"])
    assert rr.resolve_route("epic", router) is None
    assert rr.resolve_route("", router) is None
    assert rr.resolve_route("feature", None) is None


def test_route_verb_attaches_to_existing_entry(env):
    led = env["ledger"]
    led.cmd_claim("t1", "t1 title")
    led.cmd_route("t1", json.dumps({"skill": "ccos-route", "maker": "builder"}))
    entry = json.loads((env["ap_state"] / "ledger.json").read_text())["entries"]["t1"]
    assert entry["route"] == {"skill": "ccos-route", "maker": "builder"}
    assert entry["state"] == "in_progress"      # untouched — route is metadata


def test_route_verb_refuses_missing_entry(env):
    led = env["ledger"]
    with pytest.raises(SystemExit) as ex:
        led.cmd_route("ghost", json.dumps({"skill": "x"}))
    assert ex.value.code == 4
    # Nothing written: refusal happens inside _locked_update, which only
    # saves on a clean exit.
    data = json.loads((env["ap_state"] / "ledger.json").read_text()) \
        if (env["ap_state"] / "ledger.json").exists() else {"entries": {}}
    assert "ghost" not in data["entries"]


def test_route_verb_rejects_bad_json(env):
    led = env["ledger"]
    led.cmd_claim("t1", "t1 title")
    with pytest.raises(SystemExit) as ex:
        led.cmd_route("t1", "not-json")
    assert ex.value.code == 2
