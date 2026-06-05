"""CsvInvoiceSource — ingest a Magazine Manager AR/aging CSV export.

The pragmatic MVP for a billing system with no public API: a human exports the
aging report (a ~30-second click), and the deterministic core does everything
else. No credentials, no scraping, no "AI signs into the SaaS" — the lowest-risk
way to go live, and it works whether or not the publisher syncs to QuickBooks.

Real exports are messy, so the column mapping is forgiving: headers are matched
case- and punctuation-insensitively against a set of aliases ("Invoice #",
"Invoice Number", "Balance Due", "Amount Due", ...), amounts may carry "$" and
commas, and dates may be US (``MM/DD/YYYY``) or ISO. Like every source, it makes
no billing decisions — it returns the full set and lets the DunningPolicy filter.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from reminders.models import Invoice, InvoiceStatus
from reminders.sources.base import InvoiceSource


def _norm(label: str) -> str:
    """Canonicalize a header/value for matching: lowercase, alphanumerics only."""
    return "".join(ch for ch in label.lower() if ch.isalnum())


# Invoice field -> accepted header labels (compared via _norm). Order matters:
# more-specific aliases first so "Amount Due" beats a bare "Amount", etc.
_ALIASES: dict[str, list[str]] = {
    "invoice_id": ["invoiceid", "invoicenumber", "invoiceno", "invoice", "invno",
                   "docnumber", "refnumber"],
    "customer_name": ["customername", "customer", "advertiser", "client", "company",
                      "account", "name"],
    "customer_email": ["customeremail", "billingemail", "emailaddress", "email"],
    "amount": ["amountdue", "balancedue", "openbalance", "amountoutstanding",
               "totaldue", "amount", "balance", "total"],
    "currency": ["currencycode", "currency", "curr"],
    "issue_date": ["issuedate", "invoicedate", "txndate", "createddate", "date"],
    "due_date": ["duedate", "datedue"],
    "status": ["invoicestatus", "paidstatus", "status"],
    "do_not_contact": ["donotcontact", "dnc", "suppress"],
}

# currency/status/do_not_contact have safe defaults; the rest must be present.
_REQUIRED = ("invoice_id", "customer_name", "customer_email", "amount",
             "issue_date", "due_date")

_STATUS = {
    "open": InvoiceStatus.OPEN, "unpaid": InvoiceStatus.OPEN,
    "outstanding": InvoiceStatus.OPEN, "overdue": InvoiceStatus.OPEN,
    "partial": InvoiceStatus.OPEN, "partiallypaid": InvoiceStatus.OPEN,
    "paid": InvoiceStatus.PAID,
    "void": InvoiceStatus.VOID, "voided": InvoiceStatus.VOID,
    "cancelled": InvoiceStatus.VOID, "canceled": InvoiceStatus.VOID,
}

_TRUTHY = {"true", "yes", "y", "1", "x", "t"}


class DataIntegrityError(Exception):
    """A structural problem with the export that must abort an unattended run
    (e.g. a required column is missing), as opposed to a single bad row."""


class CsvInvoiceSource(InvoiceSource):
    def __init__(self, csv_path: str | Path, *, strict: bool = False):
        self.csv_path = Path(csv_path)
        # strict mode (for unattended sends): require status + do_not_contact columns
        # and quarantine rows with an UNRECOGNIZED status instead of fail-open to OPEN.
        self.strict = strict
        self.quarantined: list[tuple[str, str]] = []

    def list_open_invoices(self) -> list[Invoice]:
        self.quarantined = []
        # utf-8-sig transparently drops a BOM if the export has one.
        with self.csv_path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            mapping = self._resolve_columns(reader.fieldnames or [])
            out: list[Invoice] = []
            for row in reader:
                invoice = self._row_to_invoice(row, mapping)
                if invoice is not None:
                    out.append(invoice)
            return out

    def _resolve_columns(self, headers: list[str]) -> dict[str, str]:
        by_norm: dict[str, str] = {}
        for h in headers:
            by_norm.setdefault(_norm(h), h)  # first wins on duplicate-ish headers
        mapping: dict[str, str] = {}
        for field, aliases in _ALIASES.items():
            for alias in aliases:
                if alias in by_norm:
                    mapping[field] = by_norm[alias]
                    break
        missing = [f for f in _REQUIRED if f not in mapping]
        if missing:
            raise ValueError(
                f"CSV export is missing a recognizable column for: {', '.join(missing)}. "
                f"Found headers: {headers}"
            )
        if self.strict:
            # No fail-open: a missing status/do_not_contact column must abort, not
            # silently default every row to open/contactable.
            for field in ("status", "do_not_contact"):
                if field not in mapping:
                    raise DataIntegrityError(
                        f"unattended send requires a '{field}' column in the export; "
                        f"found headers: {headers}"
                    )
        return mapping

    def _row_to_invoice(self, row: dict, mapping: dict[str, str]) -> Invoice | None:
        def cell(field: str) -> str:
            col = mapping.get(field)
            return (row.get(col) or "").strip() if col else ""

        invoice_id = cell("invoice_id")
        if not invoice_id:
            return None  # blank/spacer row

        status_token = _norm(cell("status"))
        if status_token in _STATUS:
            status = _STATUS[status_token]
        elif self.strict:
            # Unknown/blank status: in strict mode that's do-not-send, never "open".
            self.quarantined.append((invoice_id, f"unknown-status:{cell('status')!r}"))
            return None
        else:
            status = InvoiceStatus.OPEN

        return Invoice(
            invoice_id=invoice_id,
            customer_name=cell("customer_name"),
            customer_email=cell("customer_email"),
            amount=_parse_amount(cell("amount"), invoice_id),
            currency=(cell("currency") or "USD").upper(),
            issue_date=_parse_date(cell("issue_date"), invoice_id, "issue_date"),
            due_date=_parse_date(cell("due_date"), invoice_id, "due_date"),
            status=status,
            do_not_contact=cell("do_not_contact").lower() in _TRUTHY,
        )


def _parse_amount(raw: str, invoice_id: str) -> Decimal:
    cleaned = raw.replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):  # accounting negative
        cleaned = "-" + cleaned[1:-1]
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{invoice_id}: could not parse amount {raw!r}") from None


def _parse_date(raw: str, invoice_id: str, field: str) -> date:
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):       # US export formats
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(raw)         # ISO 8601
    except ValueError:
        raise ValueError(f"{invoice_id}: could not parse {field} {raw!r}") from None
