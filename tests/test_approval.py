"""ApprovalQueue: pending batches that release only on explicit approval."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from reminders.approval import ApprovalError, ApprovalQueue, UnknownBatchError
from reminders.models import Reminder, compute_message_hash

CREATED = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)


def queue(tmp_path):
    return ApprovalQueue(str(tmp_path / "state.sqlite3"))


def reminder(invoice_id="INV-1", stage="friendly", amount="500.00"):
    body = f"Reminder for {invoice_id}"
    return Reminder(
        invoice_id=invoice_id,
        to_email="ap@acme.example",
        customer_name="Acme",
        amount=amount,
        currency="USD",
        stage=stage,
        tone=stage,
        subject=f"Reminder {invoice_id}",
        body=body,
        message_hash=compute_message_hash(body),
        days_overdue=5,
    )


def test_enqueue_then_fetch_pending_batch(tmp_path):
    q = queue(tmp_path)
    q.enqueue([reminder("INV-1"), reminder("INV-2")], batch_id="B-1", created_at=CREATED)
    batch = q.get_batch("B-1")
    assert batch.status == "pending"
    assert len(batch.reminders) == 2
    assert {r.invoice_id for r in batch.reminders} == {"INV-1", "INV-2"}


def test_not_approved_until_approved(tmp_path):
    q = queue(tmp_path)
    q.enqueue([reminder()], batch_id="B-1", created_at=CREATED)
    assert q.is_approved("B-1") is False
    q.approve("B-1")
    assert q.is_approved("B-1") is True


def test_approve_unknown_batch_raises(tmp_path):
    with pytest.raises(UnknownBatchError):
        queue(tmp_path).approve("does-not-exist")


def test_cannot_approve_twice(tmp_path):
    q = queue(tmp_path)
    q.enqueue([reminder()], batch_id="B-1", created_at=CREATED)
    q.approve("B-1")
    with pytest.raises(ApprovalError):
        q.approve("B-1")


def test_mark_sent_transitions_status(tmp_path):
    q = queue(tmp_path)
    q.enqueue([reminder()], batch_id="B-1", created_at=CREATED)
    q.approve("B-1")
    q.mark_sent("B-1")
    assert q.get_batch("B-1").status == "sent"


def test_reminder_payload_round_trips_decimal_and_body(tmp_path):
    q = queue(tmp_path)
    q.enqueue([reminder("INV-9", amount="95000.00")], batch_id="B-9", created_at=CREATED)
    r = q.get_batch("B-9").reminders[0]
    assert r.amount == Decimal("95000.00")
    assert r.body == "Reminder for INV-9"
    assert r.message_hash == compute_message_hash("Reminder for INV-9")


def test_list_batches_summarizes(tmp_path):
    q = queue(tmp_path)
    q.enqueue([reminder("INV-1"), reminder("INV-2")], batch_id="B-1", created_at=CREATED)
    q.enqueue([reminder("INV-3")], batch_id="B-2", created_at=CREATED)
    summaries = {b.batch_id: b for b in q.list_batches()}
    assert summaries["B-1"].count == 2
    assert summaries["B-2"].count == 1
    assert summaries["B-1"].status == "pending"
