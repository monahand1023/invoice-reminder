"""ReminderStateStore: idempotency enforcement + audit trail (SQLite)."""
from datetime import datetime, timezone

import pytest

from reminders.models import SendResult
from reminders.state import AlreadySentError, ReminderStateStore


def store(tmp_path):
    return ReminderStateStore(str(tmp_path / "state.sqlite3"))


def result(invoice_id="INV-1", stage="friendly", message_hash="abc123"):
    return SendResult(
        invoice_id=invoice_id,
        stage=stage,
        channel="email",
        success=True,
        detail="sent",
        message_hash=message_hash,
        sent_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
    )


def test_fresh_store_has_no_history(tmp_path):
    s = store(tmp_path)
    assert s.sent_stages("INV-1") == set()
    assert s.already_sent("INV-1", "friendly") is False
    assert s.history() == []


def test_record_then_reflected_in_queries(tmp_path):
    s = store(tmp_path)
    s.record_send(result(), to_email="ap@acme.example")
    assert s.already_sent("INV-1", "friendly") is True
    assert s.sent_stages("INV-1") == {"friendly"}


def test_record_persists_across_instances(tmp_path):
    db = str(tmp_path / "state.sqlite3")
    ReminderStateStore(db).record_send(result(), to_email="ap@acme.example")
    # A brand-new process/instance pointed at the same file must see it.
    assert ReminderStateStore(db).already_sent("INV-1", "friendly") is True


def test_duplicate_same_stage_is_rejected(tmp_path):
    s = store(tmp_path)
    s.record_send(result(), to_email="ap@acme.example")
    with pytest.raises(AlreadySentError):
        s.record_send(result(), to_email="ap@acme.example")
    # And it did not create a second row.
    assert len(s.history(invoice_id="INV-1")) == 1


def test_different_stage_same_invoice_is_allowed(tmp_path):
    s = store(tmp_path)
    s.record_send(result(stage="friendly"), to_email="ap@acme.example")
    s.record_send(result(stage="firm"), to_email="ap@acme.example")
    assert s.sent_stages("INV-1") == {"friendly", "firm"}


def test_history_filters_by_invoice_and_records_audit_fields(tmp_path):
    s = store(tmp_path)
    s.record_send(result(invoice_id="INV-1", stage="friendly", message_hash="h1"),
                  to_email="ap@acme.example", batch_id="B-1")
    s.record_send(result(invoice_id="INV-2", stage="final", message_hash="h2"),
                  to_email="ap@other.example", batch_id="B-1")

    all_rows = s.history()
    assert len(all_rows) == 2

    one = s.history(invoice_id="INV-1")
    assert len(one) == 1
    row = one[0]
    assert row.invoice_id == "INV-1"
    assert row.stage == "friendly"
    assert row.channel == "email"
    assert row.message_hash == "h1"
    assert row.to_email == "ap@acme.example"   # who it was sent to
    assert row.batch_id == "B-1"
