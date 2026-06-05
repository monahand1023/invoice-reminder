"""ReminderPipeline: source + policy + templates + state -> due reminders.

This is read-only. It computes WHAT would be sent now; it never writes state and
never sends. list-due, dry-run, and the --send enqueue step all build on it.
"""
from datetime import date

from reminders.config import SenderConfig, Stage
from reminders.models import SendResult, compute_message_hash
from reminders.pipeline import ReminderPipeline
from reminders.policy import DunningPolicy
from reminders.sources.mock import MockInvoiceSource
from reminders.state import ReminderStateStore
from reminders.templates import TemplateEngine

AS_OF = date(2026, 6, 5)
SENDER = SenderConfig(
    name="Acme Billing", email="b@acme.example", reply_to="b@acme.example",
    payment_contact="b@acme.example or 555-0000",
)
STAGES = [
    Stage(name="friendly", min_days_overdue=1, tone="friendly", template="friendly.txt.j2"),
    Stage(name="firm", min_days_overdue=14, tone="firm", template="firm.txt.j2"),
    Stage(name="final", min_days_overdue=30, tone="final", template="final.txt.j2"),
]


def build(tmp_path):
    return ReminderPipeline(
        source=MockInvoiceSource("fixtures/sample_invoices.json"),
        policy=DunningPolicy(STAGES),
        templates=TemplateEngine("templates", SENDER),
        store=ReminderStateStore(str(tmp_path / "state.sqlite3")),
    )


def test_due_set_matches_expected_stages(tmp_path):
    reminders = build(tmp_path).due_reminders(AS_OF)
    got = {r.invoice_id: r.stage for r in reminders}
    assert got == {
        "INV-1003": "friendly",
        "INV-1004": "friendly",
        "INV-1005": "friendly",
        "INV-1006": "firm",
        "INV-1007": "firm",
        "INV-1008": "firm",
        "INV-1009": "final",
        "INV-1010": "final",
    }


def test_excludes_not_due_paid_void_and_do_not_contact(tmp_path):
    ids = {r.invoice_id for r in build(tmp_path).due_reminders(AS_OF)}
    for excluded in ("INV-1001", "INV-1002", "INV-1011", "INV-1012", "INV-1013"):
        assert excluded not in ids


def test_reminders_are_rendered_with_hash(tmp_path):
    whale = next(r for r in build(tmp_path).due_reminders(AS_OF) if r.invoice_id == "INV-1010")
    assert whale.stage == "final"
    assert "INV-1010" in whale.body
    assert "95,000.00" in whale.body
    assert whale.message_hash == compute_message_hash(whale.body)


def test_already_sent_stage_is_excluded(tmp_path):
    pipeline = build(tmp_path)
    # Record that INV-1003's friendly reminder already went out.
    one = next(r for r in pipeline.due_reminders(AS_OF) if r.invoice_id == "INV-1003")
    pipeline.store.record_send(
        SendResult(invoice_id="INV-1003", stage="friendly", channel="email",
                   success=True, detail="x", message_hash=one.message_hash,
                   sent_at=__import__("datetime").datetime(2026, 6, 5)),
        to_email="x@y.example",
    )
    ids = {r.invoice_id for r in pipeline.due_reminders(AS_OF)}
    assert "INV-1003" not in ids
    assert "INV-1004" in ids   # untouched friendly still appears
