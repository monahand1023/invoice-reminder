"""cron-run: the unattended path, end to end.

Proves the guards fail closed, the cold start holds everything for review, and a
live run auto-sends only the routine lane while diverting the rest to the human
queue. No real SMTP — sends go through an injected notifier; summaries aren't
emailed (summary_to left blank).
"""
import io
import json
import os
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from reminders.approval import ApprovalQueue
from reminders.cli import cmd_cron_run, main
from reminders.config import load_config
from reminders.models import SendResult
from reminders.notifiers.base import Notifier
from reminders.state import ReminderStateStore

REPO = Path(__file__).resolve().parent.parent
SAMPLE_CSV = REPO / "fixtures" / "magazine_manager_ar_export_sample.csv"
TEMPLATES = REPO / "templates"
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


def write_csv(tmp_path, text):
    p = tmp_path / "export.csv"
    p.write_text(textwrap.dedent(text).lstrip("\n"))
    return p


def cron_config(tmp_path, csv_path, *, enabled=True, cap="friendly", **automation):
    auto = {"enabled": enabled, "summary_to": "", **automation}
    obj = {
        "sender": {"name": "Pub", "email": "b@x.example", "reply_to": "b@x.example",
                   "payment_contact": "b@x.example"},
        "templates_dir": str(TEMPLATES),
        "source": {"kind": "csv", "csv_path": str(csv_path)},
        "dunning": {"first_contact_stage_cap": cap, "stages": [
            {"name": "friendly", "min_days_overdue": 1, "tone": "friendly", "template": "friendly.txt.j2"},
            {"name": "firm", "min_days_overdue": 14, "tone": "firm", "template": "firm.txt.j2"},
            {"name": "final", "min_days_overdue": 30, "tone": "final", "template": "final.txt.j2"},
        ]},
        "state": {"db_path": str(tmp_path / "state.sqlite3")},
        "smtp": {"host": "smtp.invalid", "port": 587},
        "automation": auto,
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(obj))
    return str(p)


def cron_json(cfg, *extra, env=None):
    buf = io.StringIO()
    code = main(["--config", cfg, "--as-of", "2026-06-05", "cron-run", *extra, "--json"],
                out=buf, env=env or {})
    return code, json.loads(buf.getvalue())


# --- guards fail closed --------------------------------------------------------

def test_disabled_refuses(tmp_path):
    cfg = cron_config(tmp_path, SAMPLE_CSV, enabled=False)
    code, data = cron_json(cfg, "--dry-run")
    assert code == 2 and data["outcome"].startswith("REFUSED")


def test_missing_cap_refuses(tmp_path):
    cfg = cron_config(tmp_path, SAMPLE_CSV, cap=None)
    code, data = cron_json(cfg, "--dry-run")
    assert code == 2 and "first_contact_stage_cap" in data["outcome"]


def test_stale_export_refuses(tmp_path):
    csv = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Balance Due,Invoice Date,Due Date,Status,Do Not Contact
        INV-A,Known Co,a@x.example,300.00,2026-05-05,2026-06-04,Unpaid,No
    """)
    old = datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
    os.utime(csv, (old, old))                          # make the export ancient
    cfg = cron_config(tmp_path, csv)
    code, data = cron_json(cfg, "--dry-run")
    assert code == 2 and "stale" in data["outcome"].lower() or "old" in data["outcome"].lower()


def test_missing_status_column_refuses(tmp_path):
    csv = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Balance Due,Invoice Date,Due Date,Do Not Contact
        INV-A,Known Co,a@x.example,300.00,2026-05-05,2026-06-04,No
    """)
    cfg = cron_config(tmp_path, csv)
    code, data = cron_json(cfg, "--dry-run")
    assert code == 2 and "integrity" in data["outcome"].lower()


# --- cold start holds everything ----------------------------------------------

def test_dry_run_cold_start_holds_all_and_sends_nothing(tmp_path):
    cfg = cron_config(tmp_path, SAMPLE_CSV)
    code, data = cron_json(cfg, "--dry-run")
    assert code == 0 and data["outcome"] == "DRY-RUN"
    assert data["auto_count"] == 0          # every first-contact is held
    assert data["held"] == 3
    # dry-run records nothing
    store = ReminderStateStore(load_config(cfg).state.db_path)
    try:
        assert store.history() == []
    finally:
        store.close()


# --- live: auto-send routine, hold the rest -----------------------------------

def test_live_auto_sends_routine_and_holds_high_value(tmp_path):
    csv = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Balance Due,Invoice Date,Due Date,Status,Do Not Contact
        INV-A,Known Co,known@x.example,300.00,2026-05-05,2026-06-04,Unpaid,No
        INV-B,Whale Co,whale@x.example,95000.00,2026-04-01,2026-05-01,Unpaid,No
    """)
    cfg = cron_config(tmp_path, csv)
    config = load_config(cfg)
    # Make INV-A's recipient already-known so it is NOT held as a new advertiser.
    store = ReminderStateStore(config.state.db_path)
    try:
        store.record_send(SendResult(invoice_id="SEED", stage="friendly", channel="email",
                                     success=True, detail="seed", message_hash="h",
                                     sent_at=datetime(2026, 5, 1, tzinfo=timezone.utc)),
                          to_email="known@x.example", batch_id="seed")
    finally:
        store.close()

    fake = FakeNotifier()
    code = cmd_cron_run(config, as_of=AS_OF, env=SEATBELT, out=io.StringIO(),
                        notifier=fake, now=datetime.now(timezone.utc))
    assert code == 0
    # routine, known, under ceiling -> auto-sent; the $95K -> held
    assert [r.invoice_id for r in fake.sent] == ["INV-A"]
    held = [b for b in ApprovalQueue(config.state.db_path).list_batches()
            if b.batch_id.startswith("HELD")]
    assert len(held) == 1 and held[0].count == 1
    # audit recorded only the auto send
    store = ReminderStateStore(config.state.db_path)
    try:
        assert {r.invoice_id for r in store.history()} == {"SEED", "INV-A"}
    finally:
        store.close()


def test_paid_and_zero_amount_rows_are_quarantined(tmp_path):
    csv = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Balance Due,Invoice Date,Due Date,Status,Do Not Contact
        INV-PAID,Paid Co,p@x.example,500.00,2026-05-05,2026-06-04,Paid in Full,No
        INV-ZERO,Zero Co,z@x.example,0.00,2026-05-05,2026-06-04,Unpaid,No
        INV-OK,Ok Co,ok@x.example,200.00,2026-05-05,2026-06-04,Unpaid,No
    """)
    cfg = cron_config(tmp_path, csv)
    code, data = cron_json(cfg, "--dry-run")
    assert code == 0
    q = {inv_id for inv_id, _ in data["quarantine"]}
    assert "INV-PAID" in q          # "Paid in Full" is not a known-open status
    assert "INV-ZERO" in q          # $0.00
    assert "INV-OK" not in q
