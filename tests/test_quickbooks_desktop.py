"""QuickBooksDesktopSource — qbXML over the QuickBooks Web Connector.

QBD isn't directly queryable: the Web Connector (a SOAP *client* on the Windows
box) polls a SOAP service *we host*, exchanging qbXML. So this adapter is really:
  - a pure qbXML request builder + response parser (tested here),
  - a ``QBWCConnector`` state machine implementing the Web Connector callbacks,
    which drives one InvoiceQueryRq cycle and writes a parsed-invoice cache,
  - a read side (``QuickBooksDesktopSource``) that loads that cache,
  - a ``.qwc`` generator (the file the customer imports).

All tested offline against sample qbXML — no SOAP server, no Windows, no QuickBooks.
"""
from datetime import date
from decimal import Decimal

import pytest

from reminders.config import QBDConfig
from reminders.models import Invoice, InvoiceStatus
from reminders.sources.quickbooks_desktop import (
    QBWCConnector,
    QuickBooksDesktopSource,
    build_invoice_query_qbxml,
    generate_qwc,
    parse_invoice_query_response,
)

SAMPLE_RS = """<?xml version="1.0" ?>
<QBXML>
  <QBXMLMsgsRs>
    <InvoiceQueryRs statusCode="0" statusSeverity="Info" statusMessage="Status OK">
      <InvoiceRet>
        <TxnID>A1</TxnID>
        <RefNumber>INV-4003</RefNumber>
        <TxnDate>2026-05-05</TxnDate>
        <DueDate>2026-06-04</DueDate>
        <CustomerRef><ListID>80000001</ListID><FullName>Brightline Media</FullName></CustomerRef>
        <BalanceRemaining>3200.00</BalanceRemaining>
        <IsPaid>false</IsPaid>
      </InvoiceRet>
      <InvoiceRet>
        <TxnID>A2</TxnID>
        <RefNumber>INV-4010</RefNumber>
        <TxnDate>2026-04-01</TxnDate>
        <DueDate>2026-05-01</DueDate>
        <CustomerRef><ListID>80000002</ListID><FullName>Vanguard Automotive Holdings</FullName></CustomerRef>
        <BalanceRemaining>95000.00</BalanceRemaining>
        <IsPaid>false</IsPaid>
      </InvoiceRet>
    </InvoiceQueryRs>
  </QBXMLMsgsRs>
</QBXML>"""


# --- pure request builder ---------------------------------------------------

def test_build_request_is_qbxml_invoice_query_for_unpaid():
    xml = build_invoice_query_qbxml("13.0")
    assert '<?qbxml version="13.0"?>' in xml
    assert "<InvoiceQueryRq>" in xml
    assert "<PaidStatus>NotPaidOnly</PaidStatus>" in xml


# --- pure response parser ---------------------------------------------------

def test_parse_maps_invoice_fields():
    invoices = parse_invoice_query_response(SAMPLE_RS, home_currency="USD")
    assert [i.invoice_id for i in invoices] == ["INV-4003", "INV-4010"]
    a = invoices[0]
    assert isinstance(a, Invoice)
    assert a.amount == Decimal("3200.00")
    assert a.issue_date == date(2026, 5, 5)
    assert a.due_date == date(2026, 6, 4)
    assert a.customer_name == "Brightline Media"
    assert a.currency == "USD"               # defaulted from home_currency
    assert a.status is InvoiceStatus.OPEN     # IsPaid=false


def test_parse_marks_paid_when_ispaid_true():
    rs = SAMPLE_RS.replace("<IsPaid>false</IsPaid>", "<IsPaid>true</IsPaid>", 1)
    assert parse_invoice_query_response(rs, home_currency="USD")[0].status is InvoiceStatus.PAID


def test_parse_empty_result_is_empty_list():
    rs = '<?xml version="1.0"?><QBXML><QBXMLMsgsRs><InvoiceQueryRs statusCode="1" ' \
         'statusSeverity="Info" statusMessage="A query request did not find a matching object."/>' \
         '</QBXMLMsgsRs></QBXML>'
    assert parse_invoice_query_response(rs, home_currency="USD") == []


# --- the Web Connector callback state machine -------------------------------

def test_connector_runs_one_cycle_and_writes_cache(tmp_path):
    cache = tmp_path / "qbd_cache.json"
    conn = QBWCConnector(qbxml_version="13.0", cache_path=str(cache), home_currency="USD")

    ticket, company_file = conn.authenticate("reminders", "secret")
    assert company_file == ""                 # "" tells QBWC to use the open company file

    request = conn.sendRequestXML(ticket)
    assert "InvoiceQueryRq" in request and "NotPaidOnly" in request

    assert conn.receiveResponseXML(ticket, SAMPLE_RS) == 100   # 100% complete

    assert conn.sendRequestXML(ticket) == ""  # nothing left to ask for this session
    assert conn.closeConnection(ticket) == "OK"
    assert cache.exists()


def test_source_reads_the_cache_written_by_the_connector(tmp_path):
    cache = tmp_path / "qbd_cache.json"
    conn = QBWCConnector(qbxml_version="13.0", cache_path=str(cache), home_currency="USD")
    ticket, _ = conn.authenticate("reminders", "secret")
    conn.sendRequestXML(ticket)
    conn.receiveResponseXML(ticket, SAMPLE_RS)

    invoices = QuickBooksDesktopSource(QBDConfig(cache_path=str(cache))).list_open_invoices()
    assert {i.invoice_id for i in invoices} == {"INV-4003", "INV-4010"}
    assert next(i for i in invoices if i.invoice_id == "INV-4010").amount == Decimal("95000.00")


def test_source_errors_clearly_before_any_poll_cycle(tmp_path):
    src = QuickBooksDesktopSource(QBDConfig(cache_path=str(tmp_path / "missing.json")))
    with pytest.raises((FileNotFoundError, RuntimeError), match="(?i)web connector|poll|cache"):
        src.list_open_invoices()


# --- the .qwc the customer imports ------------------------------------------

def test_generate_qwc_has_required_tags():
    qwc = generate_qwc(
        app_name="Invoice Reminders",
        app_url="https://billing.example/qbwc",
        app_description="Pulls open invoices for dunning",
        owner_id="{57F3B9B3-86E1-4F1B-8C2A-1234567890AB}",
        file_id="{57F3B9B3-86E1-4F1B-8C2A-0987654321FE}",
        username="reminders",
    )
    for tag in ("<AppName>", "<AppURL>", "<OwnerID>", "<FileID>", "<UserName>", "</QBWCXML>"):
        assert tag in qwc
