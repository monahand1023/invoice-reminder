"""DunningPolicy — the deterministic decision core.

Given an invoice, the set of stages already sent for it, and the date to reason
about, decide whether a reminder is due and which stage.

THIS MODULE IS THE CRITICAL PATH. It must stay 100% deterministic. No LLM, no
network, no clock of its own (``as_of`` is always injected). The optional LLM
tone-rewrite lives far away from here and only touches body copy after the
stage has already been chosen.
"""
from __future__ import annotations

from datetime import date

from reminders.config import Stage
from reminders.models import Invoice, InvoiceStatus


class DunningPolicy:
    """Selects the dunning stage for an invoice, or None if no reminder is due."""

    def __init__(self, stages: list[Stage], first_contact_stage_cap: str | None = None):
        if not stages:
            raise ValueError("DunningPolicy requires at least one stage")
        # Sort ascending by threshold so callers may pass stages in any order.
        self._stages = sorted(stages, key=lambda s: s.min_days_overdue)

        # Optional cold-start / backlog safety: the FIRST reminder ever sent for
        # an invoice is held down to this stage, so a brand-new (never-contacted)
        # but very-overdue invoice doesn't open with a harsh stage. None = no cap
        # (highest-bucket-wins, the default). See README "going to production".
        self._first_contact_cap: Stage | None = None
        if first_contact_stage_cap is not None:
            by_name = {s.name: s for s in self._stages}
            if first_contact_stage_cap not in by_name:
                raise ValueError(
                    f"first_contact_stage_cap {first_contact_stage_cap!r} is not a "
                    f"configured stage; valid stages: {sorted(by_name)}"
                )
            self._first_contact_cap = by_name[first_contact_stage_cap]

    def select_stage(
        self,
        invoice: Invoice,
        sent_stage_names: set[str],
        as_of: date,
    ) -> Stage | None:
        """Return the stage to send now, or None.

        Rules (all deterministic):
          1. Only OPEN invoices are eligible (paid/void -> None).
          2. ``do_not_contact`` -> None.
          3. Pick the highest stage whose ``min_days_overdue`` <= days_overdue
             (the invoice's current "bucket"). If it isn't overdue enough for
             any stage -> None.
          4. If that bucket stage has already been sent -> None (idempotency).
             Note: a lower stage having been sent does NOT block a higher one —
             that's the intended escalation as an invoice ages.
          5. First-contact cap (optional): if NOTHING has ever been sent for this
             invoice and a cap is configured, hold the selected stage down to the
             cap. The cap can only lower the first touch, never raise it.
        """
        if invoice.status is not InvoiceStatus.OPEN:
            return None
        if invoice.do_not_contact:
            return None

        days = invoice.days_overdue(as_of)

        current: Stage | None = None
        for stage in self._stages:  # ascending; last match is the highest bucket
            if days >= stage.min_days_overdue:
                current = stage
        if current is None:
            return None

        # First-contact cap: only on the very first reminder for this invoice,
        # and only ever to pull the stage DOWN (min() by threshold), never up.
        if (
            self._first_contact_cap is not None
            and not sent_stage_names
            and current.min_days_overdue > self._first_contact_cap.min_days_overdue
        ):
            current = self._first_contact_cap

        if current.name in sent_stage_names:
            return None
        return current
