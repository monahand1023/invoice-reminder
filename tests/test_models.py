"""Tests for the data model — focus on the one piece of *behavior*: days_overdue."""
from datetime import date
from decimal import Decimal

from reminders.models import Invoice, InvoiceStatus, compute_message_hash


def make_invoice(**overrides):
    base = dict(
        invoice_id="INV-1",
        customer_name="Acme",
        customer_email="ap@acme.example",
        amount="1500.00",
        currency="USD",
        issue_date="2026-05-01",
        due_date="2026-05-06",
        status="open",
        do_not_contact=False,
    )
    base.update(overrides)
    return Invoice(**base)


def test_amount_is_exact_decimal_not_float():
    inv = make_invoice(amount="95000.00")
    assert inv.amount == Decimal("95000.00")
    assert isinstance(inv.amount, Decimal)


def test_status_parses_to_enum():
    assert make_invoice(status="open").status is InvoiceStatus.OPEN
    assert make_invoice(status="paid").status is InvoiceStatus.PAID
    assert make_invoice(status="void").status is InvoiceStatus.VOID


def test_days_overdue_after_due_date_is_positive():
    inv = make_invoice(due_date="2026-05-06")
    assert inv.days_overdue(date(2026, 6, 5)) == 30


def test_days_overdue_on_due_date_is_zero():
    inv = make_invoice(due_date="2026-06-05")
    assert inv.days_overdue(date(2026, 6, 5)) == 0


def test_days_overdue_before_due_date_is_negative():
    inv = make_invoice(due_date="2026-06-25")
    assert inv.days_overdue(date(2026, 6, 5)) == -20


def test_days_overdue_exact_boundaries():
    assert make_invoice(due_date="2026-06-04").days_overdue(date(2026, 6, 5)) == 1
    assert make_invoice(due_date="2026-05-22").days_overdue(date(2026, 6, 5)) == 14
    assert make_invoice(due_date="2026-05-06").days_overdue(date(2026, 6, 5)) == 30


def test_message_hash_is_deterministic():
    assert compute_message_hash("hello") == compute_message_hash("hello")


def test_message_hash_differs_by_content():
    assert compute_message_hash("Dear Acme, you owe $5") != compute_message_hash(
        "Dear Acme, you owe $6"
    )
