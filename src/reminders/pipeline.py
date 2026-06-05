"""ReminderPipeline — the portable, deterministic dunning core.

Wires the four pluggable pieces together and produces the list of reminders due
*right now*. It is strictly read-only: it consults the state store (to skip
stages already sent) but never writes and never sends. Writing/sending is the
caller's job, and only after approval.

This is the unit that maps onto an n8n flow: source = QuickBooks node, policy +
templates = Function node, store lookup = a dedup check. Keeping it free of I/O
side effects is what makes that port clean.
"""
from __future__ import annotations

from datetime import date

from reminders.config import SenderConfig, Stage
from reminders.models import Reminder, compute_message_hash
from reminders.policy import DunningPolicy
from reminders.sources.base import InvoiceSource
from reminders.state import ReminderStateStore
from reminders.templates import TemplateEngine


class ReminderPipeline:
    def __init__(
        self,
        source: InvoiceSource,
        policy: DunningPolicy,
        templates: TemplateEngine,
        store: ReminderStateStore,
    ):
        self.source = source
        self.policy = policy
        self.templates = templates
        self.store = store
        # Index stages by name so we can render the one the policy chose.
        self._stage_by_name: dict[str, Stage] = {s.name: s for s in policy._stages}

    def due_reminders(self, as_of: date) -> list[Reminder]:
        """Every reminder that should go out as of ``as_of``, fully rendered.

        Ordered by (stage threshold, invoice_id) for stable, legible output.
        """
        out: list[Reminder] = []
        for invoice in self.source.list_open_invoices():
            sent = self.store.sent_stages(invoice.invoice_id)
            stage = self.policy.select_stage(invoice, sent, as_of)
            if stage is None:
                continue
            msg = self.templates.render(stage, invoice, as_of)
            out.append(
                Reminder(
                    invoice_id=invoice.invoice_id,
                    to_email=invoice.customer_email,
                    customer_name=invoice.customer_name,
                    amount=invoice.amount,
                    currency=invoice.currency,
                    stage=stage.name,
                    tone=stage.tone,
                    subject=msg.subject,
                    body=msg.body,
                    message_hash=compute_message_hash(msg.body),
                    days_overdue=invoice.days_overdue(as_of),
                )
            )
        out.sort(key=lambda r: (self._stage_by_name[r.stage].min_days_overdue, r.invoice_id))
        return out
