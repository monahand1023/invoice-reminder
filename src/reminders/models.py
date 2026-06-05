"""Core data models.

Money is ``Decimal`` (never float). Dates are real ``date`` objects.
``days_overdue`` is *derived* from a caller-supplied ``as_of`` date so the whole
pipeline stays deterministic and testable — there is no hidden ``date.today()``
buried in the model.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict


class InvoiceStatus(str, Enum):
    OPEN = "open"
    PAID = "paid"
    VOID = "void"


class Invoice(BaseModel):
    """A single advertiser invoice from the billing system."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    customer_name: str
    customer_email: str
    amount: Decimal
    currency: str
    issue_date: date
    due_date: date
    status: InvoiceStatus = InvoiceStatus.OPEN
    do_not_contact: bool = False

    def days_overdue(self, as_of: date) -> int:
        """Whole days past ``due_date`` as of ``as_of``.

        Positive == overdue, 0 == due today, negative == not yet due.
        """
        return (as_of - self.due_date).days


def compute_message_hash(body: str) -> str:
    """SHA-256 of the rendered body. Stored so a re-run can prove it already
    sent *this exact* message for an (invoice, stage)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Reminder(BaseModel):
    """A fully-rendered reminder, ready for a Notifier to send. Immutable."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    to_email: str
    customer_name: str
    amount: Decimal
    currency: str
    stage: str            # stage name, e.g. "friendly"
    tone: str             # tone tier, e.g. "friendly"
    subject: str
    body: str
    message_hash: str
    days_overdue: int
    channel: str = "email"


class SendResult(BaseModel):
    """Outcome of a Notifier.send() call. Recorded to the audit trail on success."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    stage: str
    channel: str
    success: bool
    detail: str
    message_hash: str
    sent_at: datetime
