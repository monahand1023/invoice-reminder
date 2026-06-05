"""QuickBooksDesktopSource — pull open invoices from QuickBooks Desktop via qbXML.

QBD can't be queried directly. The **QuickBooks Web Connector** (QBWC) runs on the
Windows box hosting QuickBooks and polls a SOAP service *we host*, exchanging qbXML.
So this module is four cohesive parts, all tested offline:

  - ``build_invoice_query_qbxml`` / ``parse_invoice_query_response`` — the pure
    qbXML request/response logic (this is where the real protocol lives).
  - ``QBWCConnector`` — the Web Connector callback state machine. Across one poll
    cycle it hands QBWC our ``InvoiceQueryRq`` and, on the response, parses the
    invoices and writes a local JSON cache.
  - ``QuickBooksDesktopSource`` — the read side: loads that cache into Invoices.
    (The async push -> local cache -> sync read split mirrors how QBWC actually
    works, and keeps ``list_open_invoices`` a clean, side-effect-free read.)
  - ``generate_qwc`` — produces the ``.qwc`` file the customer imports into QBWC.

What a live deployment still needs: host the SOAP endpoint (e.g. spyne/Flask
wrapping ``QBWCConnector``), give the customer the generated ``.qwc``, and resolve
customer *emails* via a ``CustomerQuery`` (qbXML ``InvoiceRet`` carries no email).
Field mapping: ``RefNumber`` -> invoice_id, ``BalanceRemaining`` -> amount,
``DueDate`` -> due_date, ``TxnDate`` -> issue_date, ``CustomerRef/FullName`` ->
customer name.
"""
from __future__ import annotations

import json
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

from reminders.config import QBDConfig
from reminders.models import Invoice, InvoiceStatus
from reminders.sources.base import InvoiceSource


# --------------------------------------------------------------------------
# Pure qbXML build / parse
# --------------------------------------------------------------------------

def build_invoice_query_qbxml(qbxml_version: str = "13.0") -> str:
    """Build the qbXML ``InvoiceQueryRq`` for unpaid invoices."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<?qbxml version="{qbxml_version}"?>\n'
        '<QBXML>\n'
        '  <QBXMLMsgsRq onError="stopOnError">\n'
        '    <InvoiceQueryRq>\n'
        '      <PaidStatus>NotPaidOnly</PaidStatus>\n'
        '    </InvoiceQueryRq>\n'
        '  </QBXMLMsgsRq>\n'
        '</QBXML>'
    )


def parse_invoice_query_response(
    qbxml: str,
    *,
    home_currency: str = "USD",
    emails: dict[str, str] | None = None,
) -> list[Invoice]:
    """Parse an ``InvoiceQueryRs`` into Invoices.

    ``emails`` optionally maps customer FullName -> email (qbXML ``InvoiceRet``
    carries no email; a ``CustomerQuery`` provides it). Currency defaults to the
    company's ``home_currency`` when an invoice has no ``CurrencyRef``.
    """
    emails = emails or {}
    root = ET.fromstring(qbxml)
    out: list[Invoice] = []
    for ret in root.iter("InvoiceRet"):
        name = ret.findtext("CustomerRef/FullName", default="") or ""
        is_paid = (ret.findtext("IsPaid", default="false") or "").strip().lower() == "true"
        currency = ret.findtext("CurrencyRef/FullName") or home_currency
        out.append(
            Invoice(
                invoice_id=(ret.findtext("RefNumber", default="") or "").strip(),
                customer_name=name,
                customer_email=emails.get(name, ""),
                amount=Decimal((ret.findtext("BalanceRemaining", default="0") or "0").strip()),
                currency=currency,
                issue_date=date.fromisoformat((ret.findtext("TxnDate") or "").strip()),
                due_date=date.fromisoformat((ret.findtext("DueDate") or "").strip()),
                status=InvoiceStatus.PAID if is_paid else InvoiceStatus.OPEN,
                do_not_contact=False,
            )
        )
    return out


# --------------------------------------------------------------------------
# Web Connector callback state machine
# --------------------------------------------------------------------------

class QBWCConnector:
    """Implements the QuickBooks Web Connector SOAP callbacks for one task.

    The Web Connector calls these in order: ``authenticate`` -> ``sendRequestXML``
    -> ``receiveResponseXML`` -> ``closeConnection`` (with ``getLastError`` /
    ``connectionError`` on failure). We answer a single ``InvoiceQueryRq`` per
    session and write the parsed invoices to ``cache_path``.
    """

    def __init__(self, qbxml_version: str, cache_path: str, *,
                 home_currency: str = "USD", username: str = "", password: str = ""):
        self.qbxml_version = qbxml_version
        self.cache_path = Path(cache_path)
        self.home_currency = home_currency
        self.username = username
        self.password = password
        self._sessions: dict[str, dict] = {}

    # The Web Connector also calls serverVersion()/clientVersion(); trivial here.
    def serverVersion(self) -> str:
        return "1.0.0"

    def clientVersion(self, version: str) -> str:
        return ""  # empty = accept the connector's version

    def authenticate(self, username: str, password: str) -> list[str]:
        ticket = uuid.uuid4().hex
        ok = (not self.username) or (username == self.username and password == self.password)
        # Second element "" => use the company file currently open in QuickBooks;
        # "nvu" => invalid user (the connector stops).
        self._sessions[ticket] = {"authed": ok, "sent": False}
        return [ticket, "" if ok else "nvu"]

    def sendRequestXML(self, ticket: str, hcp_response: str = "", company_file: str = "",
                       country: str = "US", major: int = 13, minor: int = 0) -> str:
        session = self._sessions.get(ticket)
        if not session or not session["authed"] or session["sent"]:
            return ""  # nothing (more) to ask for
        session["sent"] = True
        return build_invoice_query_qbxml(self.qbxml_version)

    def receiveResponseXML(self, ticket: str, response: str, hresult: str = "",
                           message: str = "") -> int:
        if hresult:
            return -1  # error reported by the connector
        invoices = parse_invoice_query_response(response, home_currency=self.home_currency)
        self.cache_path.write_text(
            json.dumps([inv.model_dump(mode="json") for inv in invoices], indent=2)
        )
        return 100  # 100% complete -> connector closes the session

    def getLastError(self, ticket: str) -> str:
        return ""

    def connectionError(self, ticket: str, hresult: str, message: str) -> str:
        return "done"  # stop on connection error

    def closeConnection(self, ticket: str) -> str:
        self._sessions.pop(ticket, None)
        return "OK"


def generate_qwc(*, app_name: str, app_url: str, app_description: str,
                 owner_id: str, file_id: str, username: str,
                 scheduler_minutes: int = 30) -> str:
    """Produce the ``.qwc`` config file the customer imports into the Web Connector.

    ``owner_id``/``file_id`` are GUIDs you generate once. ``app_url`` is the HTTPS
    SOAP endpoint you host (wrapping ``QBWCConnector``)."""
    return (
        '<?xml version="1.0"?>\n'
        '<QBWCXML>\n'
        f'  <AppName>{app_name}</AppName>\n'
        f'  <AppID></AppID>\n'
        f'  <AppURL>{app_url}</AppURL>\n'
        f'  <AppDescription>{app_description}</AppDescription>\n'
        f'  <AppSupport>{app_url}</AppSupport>\n'
        f'  <UserName>{username}</UserName>\n'
        f'  <OwnerID>{owner_id}</OwnerID>\n'
        f'  <FileID>{file_id}</FileID>\n'
        '  <QBType>QBFS</QBType>\n'
        '  <Scheduler>\n'
        f'    <RunEveryNMinutes>{scheduler_minutes}</RunEveryNMinutes>\n'
        '  </Scheduler>\n'
        '</QBWCXML>'
    )


# --------------------------------------------------------------------------
# Read side
# --------------------------------------------------------------------------

class QuickBooksDesktopSource(InvoiceSource):
    def __init__(self, config: QBDConfig | None = None, *, cache_path: str | None = None):
        self.config = config or QBDConfig()
        self.cache_path = Path(cache_path or self.config.cache_path)

    def list_open_invoices(self) -> list[Invoice]:
        if not self.cache_path.exists():
            raise RuntimeError(
                f"No QuickBooks Desktop invoice cache at {self.cache_path}. The Web "
                "Connector must complete a poll cycle (driven by QBWCConnector) before "
                "this source has data. Confirm the customer imported the .qwc and the "
                "Web Connector has run."
            )
        rows = json.loads(self.cache_path.read_text())
        return [Invoice.model_validate(r) for r in rows]
