"""Idempotency: the same reminder is never sent twice for the same (invoice, stage),
no matter how many times the job runs."""
from datetime import date, datetime, timezone

from reminders.cli import approve_batch, due_reminders, stage_batch
from reminders.config import load_config
from reminders.models import SendResult
from reminders.notifiers.base import Notifier
from reminders.state import ReminderStateStore

AS_OF = date(2026, 6, 5)


class FakeNotifier(Notifier):
    """Records sends instead of touching SMTP."""

    channel = "email"

    def __init__(self):
        self.sent = []

    def send(self, reminder) -> SendResult:
        self.sent.append(reminder)
        return SendResult(
            invoice_id=reminder.invoice_id,
            stage=reminder.stage,
            channel=self.channel,
            success=True,
            detail="fake send",
            message_hash=reminder.message_hash,
            sent_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        )


def load(config_path):
    return load_config(config_path)


def history(config):
    store = ReminderStateStore(config.state.db_path)
    try:
        return store.history()
    finally:
        store.close()


def test_approve_records_every_send(config_path):
    config = load(config_path)
    batch = stage_batch(config, AS_OF)
    assert len(batch.reminders) == 8

    fake = FakeNotifier()
    results = approve_batch(config, batch.batch_id, notifier=fake)
    assert len(results) == 8
    assert len(fake.sent) == 8
    assert len(history(config)) == 8


def test_rerun_after_send_finds_nothing_due(config_path):
    config = load(config_path)
    approve_batch(config, stage_batch(config, AS_OF).batch_id, notifier=FakeNotifier())
    # Same day, same invoices: every current-bucket stage already sent.
    assert due_reminders(config, AS_OF) == []


def test_second_batch_after_send_is_empty(config_path):
    config = load(config_path)
    approve_batch(config, stage_batch(config, AS_OF).batch_id, notifier=FakeNotifier())

    second_batch = stage_batch(config, AS_OF)
    assert second_batch.reminders == []
    fake2 = FakeNotifier()
    assert approve_batch(config, second_batch.batch_id, notifier=fake2) == []
    assert fake2.sent == []


def test_reapproving_same_batch_does_not_resend(config_path):
    config = load(config_path)
    batch = stage_batch(config, AS_OF)
    approve_batch(config, batch.batch_id, notifier=FakeNotifier())

    # Approve the SAME batch again with a fresh notifier — nothing must go out.
    fake_again = FakeNotifier()
    results = approve_batch(config, batch.batch_id, notifier=fake_again)
    assert results == []
    assert fake_again.sent == []
    # And no duplicate audit rows were written.
    assert len(history(config)) == 8


def test_stored_hash_matches_sent_message(config_path):
    config = load(config_path)
    batch = stage_batch(config, AS_OF)
    by_id = {r.invoice_id: r for r in batch.reminders}
    approve_batch(config, batch.batch_id, notifier=FakeNotifier())

    for record in history(config):
        # The stored hash proves exactly which rendered message went out.
        assert record.message_hash == by_id[record.invoice_id].message_hash
