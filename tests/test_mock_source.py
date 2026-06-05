"""MockInvoiceSource reads the JSON seed into typed Invoice objects."""
import json
from decimal import Decimal

from reminders.models import Invoice, InvoiceStatus
from reminders.sources.mock import MockInvoiceSource

FIXTURE = "fixtures/sample_invoices.json"


def test_returns_invoice_objects():
    invoices = MockInvoiceSource(FIXTURE).list_open_invoices()
    assert invoices
    assert all(isinstance(i, Invoice) for i in invoices)


def test_returns_full_seed_including_paid_void_dnc():
    # The source does NOT make billing decisions — it returns everything and
    # lets the policy filter. paid/void/do_not_contact must be present here.
    invoices = MockInvoiceSource(FIXTURE).list_open_invoices()
    by_id = {i.invoice_id: i for i in invoices}
    assert by_id["INV-1011"].status is InvoiceStatus.PAID
    assert by_id["INV-1012"].status is InvoiceStatus.VOID
    assert by_id["INV-1013"].do_not_contact is True


def test_money_is_decimal_and_currencies_mixed():
    invoices = MockInvoiceSource(FIXTURE).list_open_invoices()
    whale = next(i for i in invoices if i.invoice_id == "INV-1010")
    assert whale.amount == Decimal("95000.00")
    currencies = {i.currency for i in invoices}
    assert {"USD", "EUR", "GBP"} <= currencies


def test_ignores_comment_and_note_metadata_keys(tmp_path):
    data = {
        "_comment": "ignore me",
        "as_of_anchor": "2026-06-05",
        "invoices": [
            {
                "invoice_id": "INV-9",
                "customer_name": "Solo",
                "customer_email": "ap@solo.example",
                "amount": "10.00",
                "currency": "USD",
                "issue_date": "2026-01-01",
                "due_date": "2026-02-01",
                "status": "open",
                "do_not_contact": False,
                "_note": "this per-invoice note must be ignored",
            }
        ],
    }
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(data))
    invoices = MockInvoiceSource(str(p)).list_open_invoices()
    assert len(invoices) == 1
    assert invoices[0].invoice_id == "INV-9"
