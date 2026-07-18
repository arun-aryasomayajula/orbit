#!/usr/bin/env python3
"""jira_to_backlog.py — Jira as a task source, with requirement enrichment.

A human applying the configured ready-label in Jira is the triage act; this
adapter turns each labeled ticket into a buildable backlog task — or into
specific questions commented back on the ticket when the requirements are too
thin to build against. Per poll (adapters run every queue rebuild):

  1. WRITEBACK (ledger → Jira, engine-side, best-effort): comment the PR URL on
     a shipped ticket, comment + optionally transition on merge, comment the
     operator's reason on reject. Never blocks the poll.
  2. IMPORT: for each labeled ticket not yet imported, run the enrichment agent
     (`claude --print "/orbit-jira-enrich <KEY>"`): it reads the exported
     ticket + surveys the code and drafts a task (context with file:line
     evidence, gradable acceptance criteria) OR a list of questions. A drafted
     task must pass the backlog lint hard gate; anything less becomes questions
     commented on the ticket (`needs_info` — retried when the ticket is next
     updated by a human).

Imports land `status: proposed` + `autopilot: human` via backlog_append, like
every adapter. The ONE sanctioned relaxation: `jira.auto_queue: true` (default
false) flips an imported, gate-passing task to queued/allow — the ready-label
WAS the human queue act. Merging stays 100% human either way.

Credentials: `$AP_STATE/.jira_token` (auto-gitignored; `$AP_HOME/.jira_token`
also honored). `basic` auth = `email:api_token` (cloud), `bearer` = a PAT
(server/DC). ALL Jira REST happens here in engine code — the enrichment agent
never receives the token, only the exported ticket JSON.

Config (`.autopilot/config.yaml`, see config/schema.yaml) — requires the
`jira:` block AND `jira` in `sources:`.

Test hooks (no network, no model):
  ORBIT_JIRA_FIXTURES=<dir>    GETs read <dir>/search.json, <dir>/transitions.json;
                               writes (comments/transitions) append to <dir>/posted.jsonl
  ORBIT_JIRA_ENRICH_CMD=<cmd>  run instead of claude (JIRA_KEY exported)

Usage:  python3 jira_to_backlog.py [--dry-run]
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

# Adapter contract (see backlog_to_tasks.run_source_adapters): AP_HOME/AP_STATE/
# ORBIT_HOME are exported and cwd is the target repo root.
if not os.environ.get("AP_HOME"):
    sys.exit("AP_HOME unset — adapters run via backlog_to_tasks (or set AP_HOME=<repo>/.autopilot)")
AP_HOME = Path(os.environ["AP_HOME"])
AP_STATE = Path(os.environ.get("AP_STATE") or AP_HOME / "state")
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ORBIT_HOME / "engine"))
from backlog_append import append_tasks, set_task_field  # noqa: E402
from backlog_lint import lint_task                        # noqa: E402

REPO = AP_HOME.parent
BACKLOG = AP_HOME / "backlog.yaml"
MAP_FILE = AP_STATE / "jira_map.json"     # key → {task_id, state, updated, …writeback flags}
TICKET_DIR = AP_STATE / "jira"
LEDGER = AP_STATE / "ledger.json"

FIXTURES = os.environ.get("ORBIT_JIRA_FIXTURES")
HTTP_TIMEOUT = 20
ENRICH_TIMEOUT = 900

PRIORITY_MAP = {"highest": "high", "high": "high", "medium": "medium"}


# --- REST client (the ONLY place Jira credentials are read) --------------------

def read_token() -> str:
    for p in (AP_STATE / ".jira_token", AP_HOME / ".jira_token"):
        try:
            tok = p.read_text().strip()
            if tok:
                return tok
        except OSError:
            pass
    return ""


def _ssl_context(cfg_all: dict) -> ssl.SSLContext | None:
    ca = ((cfg_all.get("env") or {}).get("extra_ca_certs")) or None
    return ssl.create_default_context(cafile=ca) if ca else None


def request(jcfg: dict, cfg_all: dict, method: str, path: str, payload: dict | None = None):
    """One Jira REST call → parsed JSON (or None for empty/error responses).

    Fixture mode: GETs are served from files, writes are recorded — tests run
    the full flow with zero network.
    """
    if FIXTURES:
        fx = Path(FIXTURES)
        if method == "GET":
            name = "transitions.json" if "/transitions" in path else "search.json"
            try:
                return json.loads((fx / name).read_text())
            except OSError:
                return None
        with (fx / "posted.jsonl").open("a") as f:
            f.write(json.dumps({"method": method, "path": path, "payload": payload}) + "\n")
        return {}
    token = read_token()
    if not token:
        return None
    auth = ("Bearer " + token if jcfg.get("auth") == "bearer"
            else "Basic " + base64.b64encode(token.encode()).decode())
    url = jcfg["base_url"].rstrip("/") + path
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": auth, "Accept": "application/json",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_ssl_context(cfg_all)) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"WARN: jira {method} {path} failed: {e}")
        return None


def search_ready(jcfg: dict, cfg_all: dict) -> list[dict]:
    jql = f'project = "{jcfg["project"]}" AND labels = "{jcfg.get("ready_label", "orbit-ready")}" ORDER BY updated DESC'
    q = urllib.parse.urlencode({
        "jql": jql, "maxResults": 50,
        "fields": "summary,description,comment,priority,labels,status,updated,reporter"})
    doc = request(jcfg, cfg_all, "GET", f"/rest/api/2/search?{q}")
    return list((doc or {}).get("issues") or [])


def post_comment(jcfg: dict, cfg_all: dict, key: str, body: str) -> bool:
    return request(jcfg, cfg_all, "POST", f"/rest/api/2/issue/{key}/comment",
                   {"body": body}) is not None


def transition_issue(jcfg: dict, cfg_all: dict, key: str, name: str) -> bool:
    doc = request(jcfg, cfg_all, "GET", f"/rest/api/2/issue/{key}/transitions")
    for t in (doc or {}).get("transitions") or []:
        if (t.get("name") or "").strip().lower() == name.strip().lower():
            return request(jcfg, cfg_all, "POST", f"/rest/api/2/issue/{key}/transitions",
                           {"transition": {"id": t["id"]}}) is not None
    print(f"WARN: jira {key}: no transition named {name!r} available — skipping")
    return False


# --- the enrichment agent (never sees the token) --------------------------------

def export_ticket(jcfg: dict, issue: dict) -> Path:
    """Write the agent-facing ticket JSON (fields only — no credentials)."""
    f = issue.get("fields") or {}
    key = issue["key"]
    comments = [{"author": ((c.get("author") or {}).get("displayName") or ""),
                 "body": c.get("body") or ""}
                for c in ((f.get("comment") or {}).get("comments") or [])]
    doc = {
        "key": key,
        "url": jcfg["base_url"].rstrip("/") + "/browse/" + key,
        "summary": f.get("summary") or "",
        "description": f.get("description") or "",
        "priority": ((f.get("priority") or {}).get("name") or ""),
        "labels": f.get("labels") or [],
        "status": ((f.get("status") or {}).get("name") or ""),
        "reporter": ((f.get("reporter") or {}).get("displayName") or ""),
        "comments": comments,
    }
    TICKET_DIR.mkdir(parents=True, exist_ok=True)
    out = TICKET_DIR / f"{key}.ticket.json"
    out.write_text(json.dumps(doc, indent=2) + "\n")
    return out


def run_enrich_agent(cfg_all: dict, key: str) -> dict | None:
    """Run enrichment for one ticket → parsed enrichment.json, or None."""
    out = TICKET_DIR / f"{key}.enrichment.json"
    try:
        out.unlink()
    except OSError:
        pass
    override = os.environ.get("ORBIT_JIRA_ENRICH_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        claude = shutil.which("claude")
        if not claude:
            print("WARN: claude not on PATH — cannot enrich jira tickets this poll")
            return None
        cmd = [claude, "--print", f"/orbit-jira-enrich {key}",
               "--permission-mode", cfg_all.get("permission_mode", "acceptEdits"),
               "--settings", str(ORBIT_HOME / "config" / "orbit.settings.json"),
               "--model", cfg_all.get("model", "claude-opus-4-8"),
               "--add-dir", str(ORBIT_HOME)]
    env = dict(os.environ, AP_HOME=str(AP_HOME), AP_STATE=str(AP_STATE),
               ORBIT_HOME=str(ORBIT_HOME), JIRA_KEY=key)
    timeout = int((cfg_all.get("jira") or {}).get("enrich_timeout_seconds") or ENRICH_TIMEOUT)
    try:
        subprocess.run(cmd, cwd=REPO, env=env, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"WARN: enrichment agent for {key} failed: {e}")
    try:
        return json.loads(out.read_text())
    except (OSError, ValueError):
        return None


# --- state ----------------------------------------------------------------------

def load_map() -> dict:
    try:
        return json.loads(MAP_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_map(m: dict) -> None:
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")


def ledger_entries() -> dict:
    try:
        return (json.loads(LEDGER.read_text()) or {}).get("entries") or {}
    except (OSError, ValueError):
        return {}


# --- writeback: ledger → Jira (best-effort, at most once per event) --------------

def sync_writeback(jcfg: dict, cfg_all: dict, m: dict, dry: bool) -> None:
    entries = ledger_entries()
    for key, ent in m.items():
        e = entries.get(ent.get("task_id") or "")
        if not e:
            continue
        if e.get("pr_url") and not ent.get("pr_commented"):
            msg = f"Orbit shipped this as a reviewable branch — PR: {e['pr_url']} (merge is manual)."
            print(f"  ↩ {key}: comment PR url" + (" (dry-run)" if dry else ""))
            if dry or post_comment(jcfg, cfg_all, key, msg):
                ent["pr_commented"] = True
        if e.get("state") == "merged" and not ent.get("merged_synced"):
            print(f"  ↩ {key}: merged → comment" + (" (dry-run)" if dry else ""))
            okay = dry or post_comment(jcfg, cfg_all, key,
                                       "Orbit's change for this ticket was reviewed and merged by the operator.")
            tr = jcfg.get("on_merge_transition")
            if okay and tr and not dry:
                transition_issue(jcfg, cfg_all, key, tr)
            if okay:
                ent["merged_synced"] = True
        if e.get("state") == "rejected" and not ent.get("rejected_synced"):
            note = e.get("review_note") or "(no reason recorded)"
            print(f"  ↩ {key}: rejected → comment" + (" (dry-run)" if dry else ""))
            if dry or post_comment(jcfg, cfg_all, key,
                                   f"Orbit's proposed change was rejected in review: {note}"):
                ent["rejected_synced"] = True


# --- import: Jira → backlog -------------------------------------------------------

def questions_comment(questions: list[str]) -> str:
    qs = "\n".join(f"- {q.strip()}" for q in questions if str(q).strip())
    return ("Orbit needs more information before this ticket is buildable:\n"
            f"{qs}\n\nReply in a comment — the next poll after any update retries enrichment.")


def draft_to_proposal(key: str, issue: dict, task: dict, jcfg: dict) -> dict:
    """Force the deterministic id/idempotency fields onto the agent's draft."""
    f = issue.get("fields") or {}
    jira_pri = ((f.get("priority") or {}).get("name") or "").lower()
    url = jcfg["base_url"].rstrip("/") + "/browse/" + key
    ctx = (task.get("context") or "").strip()
    return {
        "id": "jira-" + key.lower().replace("_", "-"),
        "title": (task.get("title") or f.get("summary") or key).strip(),
        "category": task.get("category") or "",
        "priority": task.get("priority") or PRIORITY_MAP.get(jira_pri, "low"),
        "context": (f"From Jira {key} ({url}).\n\n" + ctx).strip(),
        "evidence": task.get("evidence") or "",
        "acceptance_criteria": task.get("acceptance_criteria") or [],
    }


def import_issue(jcfg: dict, cfg_all: dict, issue: dict, m: dict, dry: bool) -> bool:
    """One labeled ticket → imported task, or questions on the ticket. → imported?"""
    key = issue["key"]
    updated = ((issue.get("fields") or {}).get("updated")) or ""
    export_ticket(jcfg, issue)
    enrichment = run_enrich_agent(cfg_all, key)
    if enrichment is None:
        print(f"  ✗ {key}: enrichment produced no output — will retry next poll")
        return False

    task, questions = enrichment.get("task"), list(enrichment.get("questions") or [])
    proposal = draft_to_proposal(key, issue, task, jcfg) if isinstance(task, dict) else None
    hard = lint_task(proposal)[0] if proposal else []
    if proposal and not hard and not questions:
        added, skipped = append_tasks(BACKLOG, [proposal], "jira",
                                      f"auto-ingested from Jira ({key})", dry_run=dry)
        if not added and skipped:
            print(f"  · {key}: {skipped[0][1]}")
        if not dry and added and jcfg.get("auto_queue"):
            # The sanctioned relaxation: the human's ready-label WAS the queue act.
            set_task_field(BACKLOG, proposal["id"], "status", "queued")
            set_task_field(BACKLOG, proposal["id"], "autopilot", "allow")
        state = "queued" if jcfg.get("auto_queue") else "proposed"
        print(f"  + {key} → {proposal['id']} ({state})" + (" (dry-run)" if dry else ""))
        m[key] = {"task_id": proposal["id"], "state": "imported", "updated": updated}
        return True

    # Not buildable as drafted → questions back on the ticket, retry on update.
    if proposal and hard:
        questions.append("The drafted task failed Orbit's contract gate: " + "; ".join(hard))
    if not questions:
        questions = ["The ticket lacks concrete, verifiable acceptance criteria — "
                     "what observable behaviour proves this is done?"]
    print(f"  ? {key}: needs info ({len(questions)} question(s))" + (" (dry-run)" if dry else ""))
    if not dry:
        post_comment(jcfg, cfg_all, key, questions_comment(questions))
        m[key] = {"state": "needs_info", "updated": updated,
                  "questions_asked": len(questions)}
    return False


def main() -> int:
    dry = "--dry-run" in sys.argv
    try:
        cfg_all = yaml.safe_load((AP_HOME / "config.yaml").read_text()) or {}
    except OSError:
        sys.exit("no config.yaml under AP_HOME")
    jcfg = cfg_all.get("jira")
    if not jcfg or not jcfg.get("base_url") or not jcfg.get("project"):
        print("WARN: source 'jira' configured but the jira: block (base_url, project) is "
              "missing in config.yaml — nothing polled (see config/schema.yaml)")
        return 0
    if not FIXTURES and not read_token():
        print(f"WARN: jira source configured but no token — put credentials in "
              f"{AP_STATE / '.jira_token'} (basic: email:api_token · bearer: a PAT). "
              f"Nothing polled.")
        return 0

    m = load_map()

    # 1. writeback first — cheap, and meaningful even when nothing new is labeled
    if jcfg.get("writeback", True):
        sync_writeback(jcfg, cfg_all, m, dry)

    # 2. import new / updated-after-questions tickets, bounded per poll
    issues = search_ready(jcfg, cfg_all)
    cap = int(jcfg.get("max_imports_per_poll") or 3)
    attempted = imported = 0
    for issue in issues:
        key = issue["key"]
        ent = m.get(key)
        updated = ((issue.get("fields") or {}).get("updated")) or ""
        if ent and ent.get("state") == "imported":
            continue
        if ent and ent.get("state") == "needs_info" and updated <= (ent.get("updated") or ""):
            continue  # still waiting on a human reply
        if attempted >= cap:
            print(f"  · import cap reached ({cap}/poll) — remaining tickets next poll")
            break
        attempted += 1
        if import_issue(jcfg, cfg_all, issue, m, dry):
            imported += 1

    print(f"jira: {len(issues)} labeled ticket(s) | enrichment attempted: {attempted} "
          f"| imported: {imported}")
    if not dry:
        save_map(m)
    else:
        print("(--dry-run: backlog.yaml, jira_map.json and Jira itself NOT modified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
