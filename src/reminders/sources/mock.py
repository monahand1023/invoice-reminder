"""MockInvoiceSource — the only implemented source in this pass.

Reads a seed JSON fixture into typed Invoice objects. It returns the *full*
seed set (open, paid, void, do-not-contact) unchanged: the source never makes
billing decisions, the DunningPolicy does. Keys starting with ``_`` and the
``as_of_anchor`` marker are documentation/metadata and are ignored.
"""
from __future__ import annotations

import json
from pathlib import Path

from reminders.models import Invoice
from reminders.sources.base import InvoiceSource

# Fields the Invoice model accepts; everything else in the seed is ignored.
_INVOICE_FIELDS = set(Invoice.model_fields)


class MockInvoiceSource(InvoiceSource):
    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)

    def list_open_invoices(self) -> list[Invoice]:
        data = json.loads(self.fixture_path.read_text())
        rows = data["invoices"] if isinstance(data, dict) else data
        return [
            Invoice.model_validate({k: v for k, v in row.items() if k in _INVOICE_FIELDS})
            for row in rows
        ]
