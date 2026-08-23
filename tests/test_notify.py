# The notify contract: the Slack message carries the DASHBOARD link the caller
# passed, and never the webhook URL (the webhook is a secret — embedding it in
# the message publishes it to everyone in the channel). Regression test for a
# parameter-shadowing bug that did exactly that.
#
# Run: python3 -m pytest tests/test_notify.py -v   (from orbit/)
import importlib.util
import json
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"

WEBHOOK = "https://hooks.slack.com/services/T000/B000/secret-token"
DASH = "http://127.0.0.1:8787"


def _notify(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_HOME", str(tmp_path))
    (tmp_path / ".slack_webhook").write_text(WEBHOOK + "\n")
    spec = importlib.util.spec_from_file_location("notify", ENGINE / "notify.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _capture(monkeypatch, mod):
    sent = {}

    def fake_urlopen(req, timeout=None, **kw):
        sent["to"] = req.full_url
        sent["body"] = json.loads(req.data.decode())
        class R:  # noqa: N801 - minimal stand-in
            def read(self): return b""
        return R()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_slack_message_links_the_dashboard_not_the_webhook(tmp_path, monkeypatch):
    mod = _notify(tmp_path, monkeypatch)
    sent = _capture(monkeypatch, mod)
    mod.notify_slack("🔧 Orbit shipped", "task t1 → branch", DASH)
    assert sent["to"] == WEBHOOK                       # posted TO the webhook…
    text = sent["body"]["text"]
    assert f"<{DASH}|Open the dashboard>" in text      # …linking the dashboard…
    assert WEBHOOK not in text                         # …never leaking the secret


def test_slack_message_without_url_has_no_link(tmp_path, monkeypatch):
    mod = _notify(tmp_path, monkeypatch)
    sent = _capture(monkeypatch, mod)
    mod.notify_slack("title", "message")
    assert "Open the dashboard" not in sent["body"]["text"]
    assert WEBHOOK not in sent["body"]["text"]


# ── send() dedupe (best-effort, state under $AP_STATE) ───────────────────────
def _wired(mod, monkeypatch):
    """Stub both fan-out channels and return the list send() actually reached."""
    calls = []
    monkeypatch.setattr(mod, "notify_macos", lambda t, m: calls.append((t, m)))
    monkeypatch.setattr(mod, "notify_slack", lambda t, m, u="": None)
    return calls


def test_dedupe_drops_identical_title_and_body_within_window(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_STATE", str(tmp_path))
    mod = _notify(tmp_path, monkeypatch)
    calls = _wired(mod, monkeypatch)
    mod.send("title", "body")
    mod.send("title", "body")          # same title+body, same instant — dropped
    assert len(calls) == 1


def test_dedupe_allows_a_different_body(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_STATE", str(tmp_path))
    mod = _notify(tmp_path, monkeypatch)
    calls = _wired(mod, monkeypatch)
    mod.send("title", "body one")
    mod.send("title", "body two")      # different body — not a duplicate
    assert len(calls) == 2


def test_dedupe_allows_a_repeat_after_the_window_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_STATE", str(tmp_path))
    mod = _notify(tmp_path, monkeypatch)
    calls = _wired(mod, monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: clock[0])
    mod.send("title", "body")
    clock[0] += mod.DEDUPE_WINDOW_SEC + 1
    mod.send("title", "body")          # identical, but the window has passed
    assert len(calls) == 2


def test_dedupe_never_blocks_on_a_corrupt_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_STATE", str(tmp_path))
    (tmp_path / ".notify-last").write_text("not json{")
    mod = _notify(tmp_path, monkeypatch)
    calls = _wired(mod, monkeypatch)
    mod.send("title", "body")          # must not raise despite the garbage state
    assert len(calls) == 1


def test_dedupe_never_blocks_when_state_dir_is_unwritable(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_STATE", str(tmp_path / "does-not-exist"))
    mod = _notify(tmp_path, monkeypatch)
    calls = _wired(mod, monkeypatch)
    mod.send("title", "body")          # can't persist state — must still send
    assert len(calls) == 1
