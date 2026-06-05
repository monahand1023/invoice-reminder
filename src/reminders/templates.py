"""TemplateEngine — renders stage-appropriate copy with Jinja2.

Templates live under ``templates/`` as ``*.txt.j2``. Convention: the first line
is ``Subject: ...``, then a blank line, then the body. The engine splits these
so the subject never leaks into the body.

This renders the *deterministic* baseline copy. The optional LLM tone-rewrite
(feature-flagged, OFF by default) would post-process ``body`` only — it never
runs here and never touches the subject, amount, or any selection logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from reminders.config import SenderConfig, Stage
from reminders.models import Invoice


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    body: str


class TemplateEngine:
    def __init__(self, templates_dir: str | Path, sender: SenderConfig):
        self.sender = sender
        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            undefined=StrictUndefined,   # fail loudly on a typo'd variable
            keep_trailing_newline=True,
            autoescape=False,            # plain-text emails, not HTML
        )

    def _context(self, invoice: Invoice, as_of: date) -> dict:
        return {
            "invoice_id": invoice.invoice_id,
            "customer_name": invoice.customer_name,
            "amount_display": f"{invoice.currency} {invoice.amount:,.2f}",
            "currency": invoice.currency,
            "due_date_display": invoice.due_date.isoformat(),
            "days_overdue": invoice.days_overdue(as_of),
            "payment_contact": self.sender.payment_contact,
            "sender_name": self.sender.name,
        }

    def render(self, stage: Stage, invoice: Invoice, as_of: date) -> RenderedMessage:
        template = self.env.get_template(stage.template)
        text = template.render(**self._context(invoice, as_of))
        return self._split(text)

    @staticmethod
    def _split(text: str) -> RenderedMessage:
        lines = text.splitlines()
        subject = ""
        body_start = 0
        if lines and lines[0].lower().startswith("subject:"):
            subject = lines[0].split(":", 1)[1].strip()
            body_start = 1
            # Skip exactly one blank separator line if present.
            if body_start < len(lines) and lines[body_start].strip() == "":
                body_start += 1
        body = "\n".join(lines[body_start:]).strip("\n")
        return RenderedMessage(subject=subject, body=body)
