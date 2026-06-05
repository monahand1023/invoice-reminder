"""ConsoleNotifier — prints reminders. Used by dry-run; sends nothing real.

Output is deterministic: no wall-clock timestamp or random data is printed, so
two dry-runs of the same data produce byte-identical output. The returned
SendResult carries a timestamp for the envelope, but in dry-run the pipeline
discards it and records nothing.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import TextIO

from reminders.models import Reminder, SendResult
from reminders.notifiers.base import Notifier


class ConsoleNotifier(Notifier):
    channel = "console"

    def __init__(self, stream: TextIO | None = None):
        self.stream = stream if stream is not None else sys.stdout

    def send(self, reminder: Reminder) -> SendResult:
        block = (
            "=" * 72 + "\n"
            f"[DRY-RUN] would send via console\n"
            f"  invoice : {reminder.invoice_id}  ({reminder.currency} {reminder.amount:,.2f})\n"
            f"  stage   : {reminder.stage} (tone: {reminder.tone})\n"
            f"  overdue : {reminder.days_overdue} days\n"
            f"  to      : {reminder.customer_name} <{reminder.to_email}>\n"
            f"  subject : {reminder.subject}\n"
            f"  msg_hash: {reminder.message_hash}\n"
            + "-" * 72 + "\n"
            f"{reminder.body}\n"
            + "=" * 72 + "\n"
        )
        self.stream.write(block)
        return SendResult(
            invoice_id=reminder.invoice_id,
            stage=reminder.stage,
            channel=self.channel,
            success=True,
            detail="printed to console (dry-run; nothing recorded)",
            message_hash=reminder.message_hash,
            sent_at=datetime.now(timezone.utc),
        )
