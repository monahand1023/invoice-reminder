"""The InvoiceSource interface.

The whole point of this seam: the dunning engine never knows *where* invoices
come from. Today that's a JSON fixture (MockInvoiceSource). In production it will
be exactly one of the stub adapters in this package — see the README
"OPEN QUESTION — pick the data source". Swapping the source must not touch the
policy, templates, state store, or notifiers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from reminders.models import Invoice


class InvoiceSource(ABC):
    """Read-only view of open invoices in the billing system."""

    @abstractmethod
    def list_open_invoices(self) -> list[Invoice]:
        """Return every invoice the dunning engine should consider.

        Implementations should return invoices regardless of overdue status;
        the DunningPolicy — not the source — decides who is due. Paid/void
        invoices may be included; the policy filters them. (Sources are free to
        pre-filter to ``status == open`` for efficiency, but must not make
        billing decisions.)
        """
        raise NotImplementedError
