"""STUB — Magazine Manager adapter. NOT IMPLEMENTED, and stays a stub.

Magazine Manager has **no public API**. Do NOT invent endpoints. If
the publisher's billing lives only here, the fallback is supervised browser
automation against the authenticated web UI — brittle, and a last resort. The
strongly-preferred path is to find out whether they sync to QuickBooks instead.
See README "OPEN QUESTION".
"""
from __future__ import annotations

from reminders.models import Invoice
from reminders.sources.base import InvoiceSource


class MagazineManagerSource(InvoiceSource):
    """Scrape open invoices from the Magazine Manager web UI (browser automation).

    What this adapter would need before it could be built:

    - **No API:** there is no documented/public REST or SOAP endpoint. Any
      integration is screen-scraping the logged-in UI (e.g. Playwright/Selenium).
    - **Auth:** real user credentials + whatever session/MFA the tenant enforces;
      sessions expire and must be re-established.
    - **Fragility:** selectors and the AR/aging report layout can change without
      notice; this needs monitoring and will break. Treat as last resort.
    - **Preferred alternative:** confirm whether the publisher exports/syncs to
      QuickBooks (Online or Desktop) and integrate there instead.

    Intentionally left unimplemented — no endpoints are guessed.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "MagazineManagerSource is a stub and stays one: Magazine Manager has "
            "no public API. Confirm a QuickBooks sync before considering browser "
            "automation. Do not invent endpoints."
        )

    def list_open_invoices(self) -> list[Invoice]:  # pragma: no cover - stub
        raise NotImplementedError
