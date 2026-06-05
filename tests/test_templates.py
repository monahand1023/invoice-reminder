"""TemplateEngine renders stage-appropriate copy with all required fields."""
from datetime import date

from reminders.config import SenderConfig, Stage
from reminders.models import Invoice
from reminders.templates import TemplateEngine

TEMPLATES_DIR = "templates"
AS_OF = date(2026, 6, 5)

SENDER = SenderConfig(
    name="Acme Magazine — Billing",
    email="billing@acme.example",
    reply_to="billing@acme.example",
    payment_contact="billing@acme.example or call (555) 123-4567",
)

STAGES = {
    "friendly": Stage(name="friendly", min_days_overdue=1, tone="friendly", template="friendly.txt.j2"),
    "firm": Stage(name="firm", min_days_overdue=14, tone="firm", template="firm.txt.j2"),
    "final": Stage(name="final", min_days_overdue=30, tone="final", template="final.txt.j2"),
}


def whale():
    return Invoice(
        invoice_id="INV-1010",
        customer_name="Vanguard Automotive Holdings",
        customer_email="ap@vanguardauto.example",
        amount="95000.00",
        currency="USD",
        issue_date="2026-04-01",
        due_date="2026-05-01",
        status="open",
        do_not_contact=False,
    )


def engine():
    return TemplateEngine(TEMPLATES_DIR, SENDER)


def test_body_includes_all_required_fields():
    msg = engine().render(STAGES["final"], whale(), AS_OF)
    body = msg.body
    assert "INV-1010" in body                       # invoice number
    assert "95,000.00" in body                       # amount, formatted
    assert "USD" in body                             # currency
    assert "2026-05-01" in body                      # due date
    assert "35" in body                              # days overdue (35 as of AS_OF)
    assert SENDER.payment_contact in body            # payment-contact line


def test_subject_is_separated_from_body():
    msg = engine().render(STAGES["friendly"], whale(), AS_OF)
    assert msg.subject                               # non-empty
    assert "INV-1010" in msg.subject
    assert not msg.body.lower().startswith("subject:")
    assert "Subject:" not in msg.body


def test_tone_tiers_differ():
    friendly = engine().render(STAGES["friendly"], whale(), AS_OF).body
    final = engine().render(STAGES["final"], whale(), AS_OF).body
    assert friendly != final
    assert "FINAL NOTICE" in final
    assert "FINAL NOTICE" not in friendly


def test_days_overdue_reflects_as_of():
    # Same invoice, evaluated 1 day later -> 36 days overdue.
    later = engine().render(STAGES["final"], whale(), date(2026, 6, 6))
    assert "36" in later.body
