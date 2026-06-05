"""SMTPNotifier — the ONLY component that sends real email. Heavily gated.

It is unreachable in dry-run. In ``--send`` mode it is only ever constructed
with ``allow_send=True`` after BOTH seatbelts are satisfied (the
REMINDERS_ALLOW_SEND=1 env var AND an explicit ``approve <batch-id>``). As a
final defense in depth, ``send()`` itself refuses — before opening any socket —
unless ``allow_send`` is True.
"""
from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from reminders.config import SmtpConfig
from reminders.models import Reminder, SendResult
from reminders.notifiers.base import Notifier


class SMTPNotifier(Notifier):
    channel = "email"

    def __init__(self, smtp: SmtpConfig, allow_send: bool):
        self.smtp = smtp
        self.allow_send = allow_send

    def send(self, reminder: Reminder) -> SendResult:
        if not self.allow_send:
            # Refuse BEFORE any network activity.
            raise PermissionError(
                "SMTPNotifier.send() called without allow_send=True. Real sends "
                "require REMINDERS_ALLOW_SEND=1 and an approved batch."
            )

        msg = EmailMessage()
        msg["From"] = self.smtp.username
        msg["To"] = reminder.to_email
        msg["Subject"] = reminder.subject
        msg.set_content(reminder.body)

        with smtplib.SMTP(self.smtp.host, self.smtp.port) as server:
            if self.smtp.use_tls:
                server.starttls()
            if self.smtp.username:
                server.login(self.smtp.username, self.smtp.password)
            server.send_message(msg)

        return SendResult(
            invoice_id=reminder.invoice_id,
            stage=reminder.stage,
            channel=self.channel,
            success=True,
            detail=f"sent via SMTP to {reminder.to_email}",
            message_hash=reminder.message_hash,
            sent_at=datetime.now(timezone.utc),
        )


def send_operator_email(smtp: SmtpConfig, *, to: str, subject: str, body: str) -> None:
    """Best-effort plain email to a human operator (unattended run summaries/alerts).
    Separate from the dunning path; callers should swallow failures so a mail outage
    can't break the run itself (it still exits with the right code)."""
    msg = EmailMessage()
    msg["From"] = smtp.username or to
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(smtp.host, smtp.port) as server:
        if smtp.use_tls:
            server.starttls()
        if smtp.username:
            server.login(smtp.username, smtp.password)
        server.send_message(msg)
