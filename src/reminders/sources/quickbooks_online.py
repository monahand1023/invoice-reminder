"""QuickBooksOnlineSource — pull open invoices from QuickBooks Online (REST/OAuth2).

This is the clean integration *if* the publisher's billing syncs to QuickBooks
Online. The design mirrors the rest of the project: the **pure mapping and query
flow** (QBO JSON -> Invoice, paging, customer resolution) are separated from the
**transport** (OAuth2 token refresh + HTTPS), so the logic is fully unit-tested
offline with an injected client and the network is only touched when real
credentials are configured.

What a live deployment needs (all in config / .env):
- An Intuit app: ``client_id`` / ``client_secret``.
- The OAuth2 ``refresh_token`` and the company ``realm_id``.
- ``environment``: "production" or "sandbox" (different base URLs).

Field mapping: ``DocNumber`` -> invoice_id, ``Balance`` -> amount, ``DueDate`` ->
due_date, ``TxnDate`` -> issue_date, ``CurrencyRef`` -> currency, ``CustomerRef``
-> customer (name/email resolved via a Customer query). Money is parsed as
``Decimal`` (the real client decodes JSON numbers straight to ``Decimal``).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from reminders.config import QBOConfig
from reminders.models import Invoice, InvoiceStatus
from reminders.sources.base import InvoiceSource


# --------------------------------------------------------------------------
# Pure mapping / parsing (no network, fully unit-tested)
# --------------------------------------------------------------------------

def parse_invoice_query_response(payload: dict) -> list[dict]:
    """Extract the ``Invoice`` array from a QBO query response (empty if none)."""
    return (payload.get("QueryResponse") or {}).get("Invoice", []) or []


def _customer_id(raw_invoice: dict) -> str:
    return str((raw_invoice.get("CustomerRef") or {}).get("value") or "")


def map_qbo_invoice(raw: dict, customer: dict | None) -> Invoice:
    """Map one QBO ``Invoice`` object (+ its resolved ``Customer``) to an Invoice.

    A zero ``Balance`` means nothing is owed -> treated as ``paid`` (the policy
    filters it out regardless). ``customer`` may be None before customers are
    fetched, in which case we fall back to the name on ``CustomerRef``.
    """
    customer = customer or {}
    ref = raw.get("CustomerRef") or {}
    balance = Decimal(str(raw.get("Balance", "0")))
    status = InvoiceStatus.OPEN if balance > 0 else InvoiceStatus.PAID
    return Invoice(
        invoice_id=str(raw.get("DocNumber") or raw.get("Id") or ""),
        customer_name=customer.get("DisplayName") or ref.get("name") or "",
        customer_email=(customer.get("PrimaryEmailAddr") or {}).get("Address", ""),
        amount=balance,
        currency=(raw.get("CurrencyRef") or {}).get("value", "USD"),
        issue_date=date.fromisoformat(raw["TxnDate"]),
        due_date=date.fromisoformat(raw["DueDate"]),
        status=status,
        do_not_contact=False,
    )


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------

class QuickBooksOnlineSource(InvoiceSource):
    def __init__(self, config: QBOConfig | None = None, *, client=None, page_size: int = 1000):
        self.config = config or QBOConfig()
        self._client = client          # inject a transport for tests / DI
        self.page_size = page_size

    def _ensure_client(self):
        if self._client is None:
            # Lazily build the real OAuth2/HTTPS transport; raises a clear error
            # if credentials are missing. Importing this module never requires it.
            from reminders.sources._qbo_client import QBOHttpClient

            self._client = QBOHttpClient(self.config)
        return self._client

    def list_open_invoices(self) -> list[Invoice]:
        client = self._ensure_client()
        raw_invoices = self._fetch_open_invoices(client)
        customers = self._fetch_customers(client, raw_invoices)
        return [map_qbo_invoice(inv, customers.get(_customer_id(inv))) for inv in raw_invoices]

    def _fetch_open_invoices(self, client) -> list[dict]:
        out: list[dict] = []
        pos = 1
        while True:
            stmt = (
                f"SELECT * FROM Invoice WHERE Balance > '0' "
                f"STARTPOSITION {pos} MAXRESULTS {self.page_size}"
            )
            page = parse_invoice_query_response(client.query(stmt))
            out.extend(page)
            if len(page) < self.page_size:
                break
            pos += self.page_size
        return out

    def _fetch_customers(self, client, raw_invoices: list[dict]) -> dict[str, dict]:
        ids = sorted({_customer_id(i) for i in raw_invoices if _customer_id(i)})
        if not ids:
            return {}
        in_clause = ", ".join(f"'{i}'" for i in ids)
        payload = client.query(f"SELECT * FROM Customer WHERE Id IN ({in_clause})")
        records = (payload.get("QueryResponse") or {}).get("Customer", []) or []
        return {str(c.get("Id")): c for c in records}
