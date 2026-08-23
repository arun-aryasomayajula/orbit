import subprocess, sys
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]

def _init_product(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run([sys.executable, str(ORBIT / "install" / "init.py"),
                        str(tmp_path), str(ORBIT), "--product"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return r

def test_product_scaffold_creates_workspace_dirs(tmp_path):
    _init_product(tmp_path)
    for d in ("knowledge", "insights", "research", "surveys", ".autopilot"):
        assert (tmp_path / d).is_dir(), d
    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".autopilot" / "backlog.yaml").exists()

def test_product_config_validates_and_carries_insight_gate(tmp_path):
    _init_product(tmp_path)
    cfg = (tmp_path / ".autopilot" / "config.yaml").read_text()
    assert "insight_lint" in cfg
    assert "workable_categories: [insight, funnel, hypothesis, survey_design, survey_analysis, documentation]" in cfg
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "config.py"),
                        "validate", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout

def test_product_scaffold_installs_commands_and_agents(tmp_path):
    _init_product(tmp_path)
    assert (tmp_path / ".claude" / "commands" / "orbit-insight.md").exists()
    assert (tmp_path / ".claude" / "agents" / "analyst.md").exists()

def test_product_scaffold_installs_funnel_and_hypothesis_skills(tmp_path):
    _init_product(tmp_path)
    cmd_dir = tmp_path / ".claude" / "commands"
    assert (cmd_dir / "orbit-analyze-funnel.md").exists()
    assert (cmd_dir / "orbit-hypothesis.md").exists()
    assert (cmd_dir / "orbit-survey-design.md").exists()
    assert (cmd_dir / "orbit-survey-analyze.md").exists()

def test_funnel_and_hypothesis_known_to_lint_and_emitter(monkeypatch, tmp_path):
    monkeypatch.setenv("AP_HOME", str(tmp_path / ".autopilot"))
    sys.path.insert(0, str(ORBIT / "engine"))
    import backlog_lint, backlog_to_tasks
    for cat in ("funnel", "hypothesis", "survey_design", "survey_analysis"):
        assert cat in backlog_lint.KNOWN_CATEGORIES, cat
        assert cat in backlog_to_tasks.SAFE_CATEGORIES, cat

    # Dashboard SAFE set: fresh-import command_center with AP_HOME/AP_STATE
    # monkeypatched (same pattern as the `cc` fixture in test_prd_dashboard.py)
    # so the module-level SAFE constant reflects the current source.
    import importlib
    ap_home = tmp_path / ".autopilot"
    ap_state = ap_home / "state"
    ap_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AP_STATE", str(ap_state))
    (ap_home / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')
    sys.modules.pop("command_center", None)
    cc_mod = importlib.import_module("command_center")
    importlib.reload(cc_mod)
    for cat in ("funnel", "hypothesis", "survey_design", "survey_analysis"):
        assert cat in cc_mod.SAFE, cat

def test_product_scaffold_installs_prd_skills(tmp_path):
    _init_product(tmp_path)
    prd_skills = [
        "orbit-prd-intake.md",
        "orbit-prd-research.md",
        "orbit-prd-synthesis.md",
        "orbit-prd-writer.md",
        "orbit-prd-review.md",
        "orbit-scrape.md",
    ]
    cmd_dir = tmp_path / ".claude" / "commands"
    for skill in prd_skills:
        assert (cmd_dir / skill).exists(), f"Missing PRD skill: {skill}"

def test_product_scaffold_copies_the_grounding_track(tmp_path):
    """The stage command no longer add-dirs the engine into the workspace, so
    the grounding track has to ship into .autopilot/tracks/ instead - the
    only engine file the PRD stage agents need."""
    _init_product(tmp_path)
    track = tmp_path / ".autopilot" / "tracks" / "prd-grounding.md"
    assert track.exists()
    assert track.read_text() == (ORBIT / "tracks" / "prd-grounding.md").read_text()

def test_reinit_overwrites_the_grounding_track(tmp_path):
    """Engine-owned: a stale copy from an older Orbit checkout must be
    replaced on re-init, not preserved."""
    _init_product(tmp_path)
    track = tmp_path / ".autopilot" / "tracks" / "prd-grounding.md"
    track.write_text("# stale copy from an older Orbit checkout\n")
    _init_product(tmp_path)
    assert track.read_text() == (ORBIT / "tracks" / "prd-grounding.md").read_text()

def test_code_scaffold_is_unchanged_without_flag(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([sys.executable, str(ORBIT / "install" / "init.py"),
                    str(tmp_path), str(ORBIT)], capture_output=True, text=True, check=True)
    cfg = (tmp_path / ".autopilot" / "config.yaml").read_text()
    assert "insight_lint" not in cfg
    assert not (tmp_path / "insights").exists()

def test_insight_is_a_safe_category(monkeypatch, tmp_path):
    monkeypatch.setenv("AP_HOME", str(tmp_path / ".autopilot"))
    sys.path.insert(0, str(ORBIT / "engine"))
    import backlog_to_tasks
    assert "insight" in backlog_to_tasks.SAFE_CATEGORIES

def test_init_rejects_unknown_flag(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run([sys.executable, str(ORBIT / "install" / "init.py"),
                        str(tmp_path), str(ORBIT), "--produkt"],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert not (tmp_path / ".autopilot" / "config.yaml").exists()

def test_insight_known_to_lint_and_dashboard():
    import importlib.util
    def _load(name):
        spec = importlib.util.spec_from_file_location(name, str(ORBIT / "engine" / f"{name}.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
    import os
    os.environ.setdefault("AP_HOME", "/tmp/x")  # backlog_lint has no AP_HOME need; guard anyway
    bl = _load("backlog_lint")
    assert "insight" in bl.KNOWN_CATEGORIES

def test_product_scaffold_gitkeeps_the_workspace_dirs(tmp_path):
    """git tracks files, not directories: without a .gitkeep a fresh clone of a
    committed workspace arrives with no research/, and the dashboard hides the
    whole PRD band (New PRD button included) when research/ is missing."""
    _init_product(tmp_path)
    for d in ("knowledge", "insights", "research", "surveys"):
        assert (tmp_path / d / ".gitkeep").exists(), d

def test_reinit_restores_a_missing_gitignore_entry(tmp_path):
    """A workspace scaffolded before .secrets.env existed already has an
    .autopilot/.gitignore, so a `write only if absent` init left it without the
    entry forever - one `git add -A` from committing its tokens."""
    _init_product(tmp_path)
    gi = tmp_path / ".autopilot" / ".gitignore"
    gi.write_text("# Orbit secrets - never commit\n.jira_token\n.slack_webhook\n")
    _init_product(tmp_path)   # re-running init must top up what is missing
    lines = gi.read_text().splitlines()
    assert ".secrets.env" in lines
    assert ".jira_token" in lines and ".slack_webhook" in lines

def test_reinit_is_idempotent_on_a_complete_gitignore(tmp_path):
    _init_product(tmp_path)
    gi = tmp_path / ".autopilot" / ".gitignore"
    before = gi.read_text()
    _init_product(tmp_path)
    assert gi.read_text() == before   # byte-identical: nothing appended twice

def test_reinit_preserves_unrelated_gitignore_lines(tmp_path):
    _init_product(tmp_path)
    gi = tmp_path / ".autopilot" / ".gitignore"
    gi.write_text("scratch/\n.jira_token\n")
    _init_product(tmp_path)
    lines = gi.read_text().splitlines()
    assert lines[0] == "scratch/"
    assert set(lines) >= {"scratch/", ".jira_token", ".slack_webhook", ".secrets.env"}

def test_router_yaml_parses_and_has_funnel_hypothesis_categories():
    import yaml
    router = yaml.safe_load((ORBIT / "router" / "router.yaml").read_text())
    cats = router.get("categories", {})
    for cat in ("funnel", "hypothesis", "survey_design", "survey_analysis"):
        assert cat in cats, cat
        assert cats[cat].get("maker", {}).get("agent") == "analyst"
        assert cats[cat].get("maker", {}).get("model") == "opus"

def test_doctor_warns_on_missing_mcp_token_env(tmp_path):
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\nmcp:\n  metaql:\n    transport: http\n'
                   '    url: "https://m/mcp"\n    token_env: METAQL_TOKEN\n    tools: "*"\n')
    import os
    env = dict(os.environ, ORBIT_HOME=str(ORBIT))
    env.pop("METAQL_TOKEN", None)
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)
    assert "METAQL_TOKEN" in r.stdout

def test_figma_mcp_stanza_scaffolded_in_product_template():
    """install/init.py's PRODUCT config template's commented mcp: example must
    show a figma stanza alongside metaql, so a workspace operator sees how to
    declare one without reading the schema doc separately."""
    src = (ORBIT / "install" / "init.py").read_text()
    assert "#   figma:" in src
    assert '#     args: ["-y", "figma-mcp"]' in src

def test_figma_sources_paragraph_in_prd_research_skill():
    text = (ORBIT / "skills" / "orbit-prd-research.md").read_text()
    assert "figma" in text.lower()
    assert "kind: figma" in text
