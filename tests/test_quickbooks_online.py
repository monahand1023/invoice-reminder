"""QuickBooksOnlineSource — the QBO REST adapter, the cleanest path *if* the
publisher syncs to QuickBooks Online.

The mapping (QBO JSON -> Invoice) and the query/paging flow are tested here
against sample payloads with an injected transport — no OAuth2, no network, no
credentials. The real transport (urllib) is only built when no client is injected
and credentials are present.
"""
from datetime import date
from decimal import Decimal

import pytest

from reminders.config import QBOConfig
from reminders.models import Invoice, InvoiceStatus
from reminders.sources.quickbooks_online import (
    QuickBooksOnlineSource,
    map_qbo_invoice,
    parse_invoice_query_response,
)

INVOICE = {
    "Id": "130", "DocNumber": "INV-3003", "TxnDate": "2026-05-05", "DueDate": "2026-06-04",
    "Balance": 3200.0, "TotalAmt": 3200.0,
    "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
    "CustomerRef": {"value": "58", "name": "Brightline Media"},
}
PAID_INVOICE = {
    "Id": "131", "DocNumber": "INV-3004", "TxnDate": "2026-05-01", "DueDate": "2026-05-31",
    "Balance": 0, "TotalAmt": 1000.0,
    "CurrencyRef": {"value": "USD"}, "CustomerRef": {"value": "58", "name": "Brightline Media"},
}
CUSTOMER = {
    "Id": "58", "DisplayName": "Brightline Media",
    "PrimaryEmailAddr": {"Address": "accounts@brightline.example"},
}


class FakeQBOClient:
    """Dispatches on the SQL-ish statement, paging Invoice by STARTPOSITION."""

    def __init__(self, invoice_pages, customers, page_size=2):
        self.invoice_pages = invoice_pages
        self.customers = customers
        self.page_size = page_size
        self.statements = []

    def query(self, statement):
        self.statements.append(statement)
        if "FROM Customer" in statement:
            return {"QueryResponse": {"Customer": self.customers}}
        import re
        pos = int(re.search(r"STARTPOSITION (\d+)", statement).group(1))
        idx = (pos - 1) // self.page_size
        page = self.invoice_pages[idx] if idx < len(self.invoice_pages) else []
        return {"QueryResponse": {"Invoice": page}}


# --- pure mapping -----------------------------------------------------------

def test_map_invoice_fields():
    inv = map_qbo_invoice(INVOICE, CUSTOMER)
    assert isinstance(inv, Invoice)
    assert inv.invoice_id == "INV-3003"
    assert inv.amount == Decimal("3200.00")
    assert inv.currency == "USD"
    assert inv.issue_date == date(2026, 5, 5)
    assert inv.due_date == date(2026, 6, 4)
    assert inv.customer_name == "Brightline Media"
    assert inv.customer_email == "accounts@brightline.example"
    assert inv.status is InvoiceStatus.OPEN


def test_map_zero_balance_is_paid():
    assert map_qbo_invoice(PAID_INVOICE, CUSTOMER).status is InvoiceStatus.PAID


def test_map_without_customer_record_falls_back_to_ref_name():
    inv = map_qbo_invoice(INVOICE, None)
    assert inv.customer_name == "Brightline Media"   # from CustomerRef.name
    assert inv.customer_email == ""                  # unknown until customer fetched


def test_parse_query_response_empty_when_no_invoices():
    assert parse_invoice_query_response({"QueryResponse": {}}) == []


# --- list_open_invoices via injected transport ------------------------------

def test_list_open_invoices_maps_and_resolves_customers():
    client = FakeQBOClient(invoice_pages=[[INVOICE]], customers=[CUSTOMER], page_size=2)
    invoices = QuickBooksOnlineSource(QBOConfig(), client=client, page_size=2).list_open_invoices()
    assert [i.invoice_id for i in invoices] == ["INV-3003"]
    assert invoices[0].customer_email == "accounts@brightline.example"
    # It must have queried customers by the referenced id.
    assert any("FROM Customer" in s and "'58'" in s for s in client.statements)


def test_list_open_invoices_pages_until_short_page():
    a = dict(INVOICE, Id="1", DocNumber="INV-A", CustomerRef={"value": "58", "name": "B"})
    b = dict(INVOICE, Id="2", DocNumber="INV-B", CustomerRef={"value": "58", "name": "B"})
    c = dict(INVOICE, Id="3", DocNumber="INV-C", CustomerRef={"value": "58", "name": "B"})
    # page_size 2 -> first page full [a,b], second page partial [c] -> stop.
    client = FakeQBOClient(invoice_pages=[[a, b], [c]], customers=[CUSTOMER], page_size=2)
    invoices = QuickBooksOnlineSource(QBOConfig(), client=client, page_size=2).list_open_invoices()
    assert [i.invoice_id for i in invoices] == ["INV-A", "INV-B", "INV-C"]


# --- safety: import-safe, clear error without credentials -------------------

def test_constructs_without_credentials_but_errors_on_use():
    src = QuickBooksOnlineSource(QBOConfig())   # must NOT raise at construction
    with pytest.raises((ValueError, RuntimeError), match="(?i)credential|client_id|realm"):
        src.list_open_invoices()                # no client injected, no creds -> clear error
