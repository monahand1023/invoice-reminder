"""--json output mode: the machine-readable surface for scripting / monitoring.

Every command emits a single JSON object on stdout so a script can parse batch
ids, counts, and results instead of scraping human tables.
"""
import io
import json
from datetime import datetime, timezone

from reminders.cli import cmd_approve, main, stage_batch
from reminders.config import load_config
from reminders.models import SendResult
from reminders.notifiers.base import Notifier

AS_OF = "2026-06-05"
SEATBELT = {"REMINDERS_ALLOW_SEND": "1"}


class FakeNotifier(Notifier):
    channel = "email"

    def __init__(self):
        self.sent = []

    def send(self, reminder) -> SendResult:
        self.sent.append(reminder)
        return SendResult(
            invoice_id=reminder.invoice_id, stage=reminder.stage, channel=self.channel,
            success=True, detail="fake send", message_hash=reminder.message_hash,
            sent_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        )


def run_cli(config_path, *args, env=None):
    buf = io.StringIO()
    code = main(["--config", config_path, "--as-of", AS_OF, "--json", *args],
                out=buf, env=env or {})
    return code, json.loads(buf.getvalue())


def test_list_due_json(config_path):
    code, data = run_cli(config_path, "list-due")
    assert code == 0
    assert data["count"] == data["count"] and data["count"] > 0
    r = data["reminders"][0]
    assert {"invoice_id", "stage", "amount", "currency", "to_email", "days_overdue"} <= r.keys()
    assert isinstance(r["amount"], str)        # Decimal serialized as string (exact)


def test_run_dry_run_json_marks_mode(config_path):
    code, data = run_cli(config_path, "run")   # dry-run is the default
    assert code == 0
    assert data["mode"] == "dry-run"
    assert data["count"] > 0


def test_run_send_json_returns_batch_id(config_path):
    code, data = run_cli(config_path, "run", "--send", env=SEATBELT)
    assert code == 0
    assert data["batch_id"].startswith("B-")
    assert data["count"] > 0


def test_run_send_json_without_seatbelt_is_error(config_path):
    code, data = run_cli(config_path, "run", "--send", env={})   # no seatbelt
    assert code == 2
    assert data["error"] == "seatbelt_required"


def test_approve_json_reports_sent(config_path):
    config = load_config(config_path)
    batch = stage_batch(config, __import__("datetime").date(2026, 6, 5))
    buf = io.StringIO()
    code = cmd_approve(config, batch_id=batch.batch_id, env=SEATBELT, out=buf,
                       notifier=FakeNotifier(), as_json=True)
    data = json.loads(buf.getvalue())
    assert code == 0
    assert data["batch_id"] == batch.batch_id
    assert data["sent"] == len(batch.reminders)
    assert {"invoice_id", "stage", "channel"} <= data["results"][0].keys()


def test_history_json_after_a_send(config_path):
    config = load_config(config_path)
    batch = stage_batch(config, __import__("datetime").date(2026, 6, 5))
    cmd_approve(config, batch_id=batch.batch_id, env=SEATBELT, out=io.StringIO(),
                notifier=FakeNotifier(), as_json=True)
    code, data = run_cli(config_path, "history")
    assert code == 0
    assert data["count"] == len(batch.reminders)
    assert {"invoice_id", "stage", "message_hash"} <= data["records"][0].keys()
