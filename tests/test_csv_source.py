"""CsvInvoiceSource — the runnable MVP that ingests a Magazine Manager AR/aging
export. No API, no scraping: a human exports a CSV and the deterministic core does
the rest. These tests pin the column mapping and value parsing against a realistic,
messy export (US dates, $-and-comma amounts, "Unpaid"/"Paid", header aliases).

Like every source, it must NOT make billing decisions — it returns the full set
(open/paid/void/do-not-contact) and lets the DunningPolicy filter.
"""
import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from reminders.models import Invoice, InvoiceStatus
from reminders.sources.csv_source import CsvInvoiceSource

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "fixtures" / "magazine_manager_ar_export_sample.csv"


def write_csv(tmp_path, text):
    p = tmp_path / "export.csv"
    p.write_text(textwrap.dedent(text).lstrip("\n"))
    return str(p)


# --- the committed sample (this is what a demo runs against) ----------------

def test_sample_export_parses_into_invoices():
    invoices = CsvInvoiceSource(str(SAMPLE)).list_open_invoices()
    assert len(invoices) == 6
    assert all(isinstance(i, Invoice) for i in invoices)


def test_sample_money_is_decimal_and_dollar_signs_stripped():
    by_id = {i.invoice_id: i for i in CsvInvoiceSource(str(SAMPLE)).list_open_invoices()}
    assert by_id["INV-2010"].amount == Decimal("95000.00")   # "$95,000.00"
    assert by_id["INV-2003"].amount == Decimal("3200.00")


def test_sample_us_dates_parsed():
    by_id = {i.invoice_id: i for i in CsvInvoiceSource(str(SAMPLE)).list_open_invoices()}
    assert by_id["INV-2003"].due_date == date(2026, 6, 4)     # "06/04/2026"
    assert by_id["INV-2003"].issue_date == date(2026, 5, 5)


def test_sample_currency_defaults_to_usd_when_column_absent():
    invoices = CsvInvoiceSource(str(SAMPLE)).list_open_invoices()
    assert all(i.currency == "USD" for i in invoices)


def test_sample_returns_full_set_including_paid_and_dnc():
    by_id = {i.invoice_id: i for i in CsvInvoiceSource(str(SAMPLE)).list_open_invoices()}
    assert by_id["INV-2011"].status is InvoiceStatus.PAID
    assert by_id["INV-2013"].do_not_contact is True


# --- header aliases / messiness --------------------------------------------

def test_header_aliases_are_case_and_label_insensitive(tmp_path):
    path = write_csv(tmp_path, """
        Invoice Number,Customer,E-mail,Amount Due,Currency,Date,Due Date,Status,DNC
        INV-9,Solo Co,ap@solo.example,1000.00,EUR,2026-01-01,2026-02-01,Outstanding,false
    """)
    inv = CsvInvoiceSource(path).list_open_invoices()[0]
    assert inv.invoice_id == "INV-9"
    assert inv.customer_name == "Solo Co"
    assert inv.customer_email == "ap@solo.example"
    assert inv.amount == Decimal("1000.00")
    assert inv.currency == "EUR"
    assert inv.status is InvoiceStatus.OPEN          # "Outstanding" normalizes to open
    assert inv.do_not_contact is False


def test_status_normalization(tmp_path):
    path = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Balance Due,Invoice Date,Due Date,Status,Do Not Contact
        A,X,x@x.example,1.00,01/01/2026,02/01/2026,Unpaid,No
        B,Y,y@y.example,2.00,01/01/2026,02/01/2026,PAID,No
        C,Z,z@z.example,3.00,01/01/2026,02/01/2026,Void,No
    """)
    by_id = {i.invoice_id: i for i in CsvInvoiceSource(path).list_open_invoices()}
    assert by_id["A"].status is InvoiceStatus.OPEN
    assert by_id["B"].status is InvoiceStatus.PAID
    assert by_id["C"].status is InvoiceStatus.VOID


def test_iso_and_us_dates_both_supported(tmp_path):
    path = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Balance Due,Invoice Date,Due Date,Status,Do Not Contact
        ISO,X,x@x.example,1.00,2026-03-21,2026-04-20,Unpaid,No
        US,Y,y@y.example,2.00,03/21/2026,04/20/2026,Unpaid,No
    """)
    by_id = {i.invoice_id: i for i in CsvInvoiceSource(path).list_open_invoices()}
    assert by_id["ISO"].due_date == date(2026, 4, 20)
    assert by_id["US"].due_date == date(2026, 4, 20)


def test_do_not_contact_truthy_variants(tmp_path):
    path = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Balance Due,Invoice Date,Due Date,Status,Do Not Contact
        T1,X,x@x.example,1.00,01/01/2026,02/01/2026,Unpaid,Yes
        T2,Y,y@y.example,2.00,01/01/2026,02/01/2026,Unpaid,TRUE
        T3,Z,z@z.example,3.00,01/01/2026,02/01/2026,Unpaid,
    """)
    by_id = {i.invoice_id: i for i in CsvInvoiceSource(path).list_open_invoices()}
    assert by_id["T1"].do_not_contact is True
    assert by_id["T2"].do_not_contact is True
    assert by_id["T3"].do_not_contact is False


def test_blank_rows_are_skipped(tmp_path):
    path = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Balance Due,Invoice Date,Due Date,Status,Do Not Contact
        A,X,x@x.example,1.00,01/01/2026,02/01/2026,Unpaid,No

        ,,,,,,,
    """)
    invoices = CsvInvoiceSource(path).list_open_invoices()
    assert len(invoices) == 1


def test_missing_required_column_is_a_clear_error(tmp_path):
    # No recognizable amount column.
    path = write_csv(tmp_path, """
        Invoice #,Advertiser,Billing Email,Invoice Date,Due Date,Status,Do Not Contact
        A,X,x@x.example,01/01/2026,02/01/2026,Unpaid,No
    """)
    with pytest.raises(ValueError, match="amount"):
        CsvInvoiceSource(path).list_open_invoices()
