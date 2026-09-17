"""route_resolve.py — resolve a task category to its route.

One resolution rule, shared by the claim-time recorder (route_record.py) and
the dashboard (command_center.py): the target's .autopilot/router.yaml wins;
otherwise the engine default router/router.yaml applies. This mirrors the
orchestrator's own read (skills/orbit-cycle.md) and doctor.py's check, so the
portal shows the same route the cycle will use. Pure reads — this module
never writes anything.
"""
from pathlib import Path

import yaml

ENGINE_DEFAULT = Path(__file__).resolve().parent.parent / "router" / "router.yaml"


def router_path(ap_home):
    """(path, source) — source is "override" | "default" | "missing"."""
    if ap_home:
        p = Path(ap_home) / "router.yaml"
        if p.is_file():
            return p, "override"
    if ENGINE_DEFAULT.is_file():
        return ENGINE_DEFAULT, "default"
    return None, "missing"


def _normalize(cat, raw, goldens):
    # Flatten one categories.<cat> block to a stable, flat route dict. The
    # golden lives at the router's top level (goldens.by_category), not in
    # the category block, so it is joined in here.
    maker = raw.get("maker") or {}
    by_cat = (goldens.get("by_category") or {}) if isinstance(goldens, dict) else {}
    return {"category": cat,
            "maker": maker.get("agent"),
            "model": maker.get("model"),
            "effort": maker.get("effort"),
            "skill": raw.get("skill"),
            "tracks": raw.get("tracks") or [],
            "policy": raw.get("policy"),
            "golden": by_cat.get(cat)}


def load_router(ap_home):
    """{"path", "source", "categories": {cat: route}} — or None when no
    router file exists or it fails to parse (callers degrade to no route)."""
    path, source = router_path(ap_home)
    if path is None:
        return None
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    goldens = doc.get("goldens") or {}
    cats = doc.get("categories") or {}
    if not isinstance(cats, dict):
        return None
    return {"path": str(path), "source": source,
            "categories": {c: _normalize(c, r or {}, goldens)
                           for c, r in cats.items()}}


def resolve_route(category, router):
    """The normalized route for a category, or None (unknown / no router)."""
    if not router or not category:
        return None
    return (router.get("categories") or {}).get(category)
