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
