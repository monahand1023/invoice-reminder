"""Notifier behavior: ConsoleNotifier prints deterministically; SMTPNotifier is gated."""
import io

import pytest

from reminders.config import SmtpConfig
from reminders.models import Reminder, compute_message_hash
from reminders.notifiers.console import ConsoleNotifier
from reminders.notifiers.smtp import SMTPNotifier


def reminder():
    body = "Hi Acme, invoice INV-7 for USD 500.00 is 5 days overdue. Contact billing@x."
    return Reminder(
        invoice_id="INV-7",
        to_email="ap@acme.example",
        customer_name="Acme",
        amount="500.00",
        currency="USD",
        stage="friendly",
        tone="friendly",
        subject="Friendly reminder: invoice INV-7 (USD 500.00)",
        body=body,
        message_hash=compute_message_hash(body),
        days_overdue=5,
    )


def test_console_send_returns_success_on_console_channel():
    out = io.StringIO()
    result = ConsoleNotifier(stream=out).send(reminder())
    assert result.success is True
    assert result.channel == "console"
    assert result.invoice_id == "INV-7"
    assert result.stage == "friendly"


def test_console_prints_key_fields():
    out = io.StringIO()
    ConsoleNotifier(stream=out).send(reminder())
    printed = out.getvalue()
    assert "INV-7" in printed
    assert "ap@acme.example" in printed
    assert "Friendly reminder" in printed       # subject
    assert "5 days overdue" in printed           # body content
    assert "friendly" in printed                 # stage/tone


def test_console_output_is_deterministic_across_runs():
    # No wall-clock or random data may leak into printed dry-run output.
    a, b = io.StringIO(), io.StringIO()
    ConsoleNotifier(stream=a).send(reminder())
    ConsoleNotifier(stream=b).send(reminder())
    assert a.getvalue() == b.getvalue()


def smtp_cfg():
    return SmtpConfig(host="smtp.example", port=587, use_tls=True,
                      username="u@example", password="secret")


def test_smtp_channel_is_email():
    assert SMTPNotifier(smtp_cfg(), allow_send=False).channel == "email"


def test_smtp_refuses_to_send_when_not_allowed():
    # Defense in depth: even if reached, SMTPNotifier won't touch the network
    # unless explicitly allowed. Must raise BEFORE any connection attempt.
    notifier = SMTPNotifier(smtp_cfg(), allow_send=False)
    with pytest.raises(PermissionError):
        notifier.send(reminder())
