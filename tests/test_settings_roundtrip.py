# The Admin panel's Save must never silently DELETE product config.
#
# The panel renders one checkbox per option the server serves in
# meta.categories_all / meta.sources_all, and Save posts back exactly the
# checked boxes (cc_shell.html's collectChecks). So any value that lives in
# config.yaml but is missing from the served meta has no checkbox, is never
# collected, and is wiped by the first Save - which is how a product
# workspace (workable_categories: [insight, funnel, hypothesis,
# survey_design, survey_analysis, documentation], sources: [backlog,
# metric_watch]) used to lose its whole product setup on an unrelated edit.
#
# Two invariants pinned here: the constants NAME the product categories and
# sources, and config_payload structurally serves the UNION of the constants
# and what the config already holds - so even an option no build knows about
# still renders and still round-trips.
#
# Run: python3 -m pytest tests/test_settings_roundtrip.py -v   (from orbit/)
import importlib
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"

# The product-workspace config install/init.py --product writes.
PRODUCT_CFG = (
    'repo: "."\n'
    'base_branch: "main"\n'
    "gates:\n"
    "  insight_lint:\n"
    "    cmd: 'python3 engine/insight_lint.py .'\n"
    "workable_categories: [insight, funnel, hypothesis, survey_design, survey_analysis, documentation]\n"
    "sources: [backlog, metric_watch]\n"
)


def _cc(tmp_path, monkeypatch, cfg_text):
    ap_home = tmp_path / ".autopilot"
    (ap_home / "state").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_home / "state"))
    (ap_home / "config.yaml").write_text(cfg_text)
    sys.path.insert(0, str(ENGINE))
    try:
        sys.modules.pop("command_center", None)
        return importlib.import_module("command_center")
    finally:
        sys.path.remove(str(ENGINE))


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    return _cc(tmp_path, monkeypatch, PRODUCT_CFG)


def _save_payload(payload):
    """What the Admin panel would POST: one checkbox per served option,
    checked iff the option is in the config list (cc_shell.html's `checks`
    renderer), collected back with collectChecks()."""
    cfg, meta = payload["config"], payload["meta"]
    return {
        "workable_categories": [o for o in meta["categories_all"]
                                if o in (cfg.get("workable_categories") or [])],
        "sources": [o for o in meta["sources_all"] if o in (cfg.get("sources") or [])],
    }


def test_product_categories_and_sources_round_trip_unchanged(cc):
    """Sorted comparison: Save posts back the served checkbox order, so the
    SET is what must round-trip - a reordering is not a loss, a missing
    entry is."""
    payload = cc.config_payload()
    sent = _save_payload(payload)
    assert sorted(sent["workable_categories"]) == sorted(payload["config"]["workable_categories"])
    assert sorted(sent["sources"]) == sorted(payload["config"]["sources"])


def test_constants_name_every_product_category_and_source(cc):
    for cat in ("insight", "funnel", "hypothesis", "survey_design", "survey_analysis", "design_review"):
        assert cat in cc.CATEGORIES_ALL
    assert "metric_watch" in cc.SOURCES_ALL


def test_unknown_config_value_still_gets_a_checkbox(tmp_path, monkeypatch):
    """The structural half: a value NO build knows about must still render
    (so it stays checked) rather than being dropped by omission."""
    cfg_text = PRODUCT_CFG + "\n"
    cfg_text = cfg_text.replace(
        "workable_categories: [insight,",
        "workable_categories: [future_category, insight,")
    cfg_text = cfg_text.replace("sources: [backlog,", "sources: [future_source, backlog,")
    cc = _cc(tmp_path, monkeypatch, cfg_text)
    payload = cc.config_payload()
    assert "future_category" not in cc.CATEGORIES_ALL      # genuinely unknown
    assert "future_category" in payload["meta"]["categories_all"]
    assert "future_source" in payload["meta"]["sources_all"]
    sent = _save_payload(payload)
    assert sorted(sent["workable_categories"]) == sorted(payload["config"]["workable_categories"])
    assert sorted(sent["sources"]) == sorted(payload["config"]["sources"])
