"""`batches` command — visibility + GC for the persisted approval queue.

The daily `run --send` stages a NEW pending batch even if prior ones were never
approved; without a way to see or discard them they pile up silently. `batches`
lists them and `--cancel` discards one. A canceled batch can never be sent.
"""
import io
from datetime import date, datetime, timezone

from reminders.cli import approve_batch, cmd_approve, main, stage_batch
from reminders.config import load_config
from reminders.models import SendResult
from reminders.notifiers.base import Notifier

AS_OF = date(2026, 6, 5)
SEATBELT = {"REMINDERS_ALLOW_SEND": "1"}


class FakeNotifier(Notifier):
    channel = "email"

    def __init__(self):
        self.sent = []

    def send(self, reminder) -> SendResult:
        self.sent.append(reminder)
        return SendResult(invoice_id=reminder.invoice_id, stage=reminder.stage,
                          channel=self.channel, success=True, detail="fake",
                          message_hash=reminder.message_hash,
                          sent_at=datetime(2026, 6, 5, tzinfo=timezone.utc))


def batches_json(config_path, *extra):
    buf = io.StringIO()
    code = main(["--config", config_path, "--json", "batches", *extra], out=buf, env={})
    import json
    return code, json.loads(buf.getvalue())


def test_batches_lists_staged_batches(config_path):
    config = load_config(config_path)
    stage_batch(config, AS_OF, batch_id="B-001")
    stage_batch(config, AS_OF, batch_id="B-002")
    code, data = batches_json(config_path)
    assert code == 0
    ids = {b["batch_id"]: b["status"] for b in data["batches"]}
    assert ids == {"B-001": "pending", "B-002": "pending"}


def test_cancel_marks_batch_canceled(config_path):
    config = load_config(config_path)
    stage_batch(config, AS_OF, batch_id="B-001")
    code, data = batches_json(config_path, "--cancel", "B-001")
    assert code == 0 and data["canceled"] == "B-001"
    _, listing = batches_json(config_path)
    assert listing["batches"][0]["status"] == "canceled"


def test_canceled_batch_cannot_be_sent(config_path):
    config = load_config(config_path)
    stage_batch(config, AS_OF, batch_id="B-001")
    batches_json(config_path, "--cancel", "B-001")
    fake = FakeNotifier()
    code = cmd_approve(config, batch_id="B-001", env=SEATBELT, out=io.StringIO(), notifier=fake)
    assert code == 1            # refused
    assert fake.sent == []      # nothing went out


def test_cancel_unknown_batch_is_an_error(config_path):
    code, data = batches_json(config_path, "--cancel", "B-nope")
    assert code == 1
    assert data["error"] == "cancel_failed"
