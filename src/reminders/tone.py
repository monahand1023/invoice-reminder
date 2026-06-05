"""ToneRewriter — the ONLY place an LLM touches a reminder, and it touches *tone* only.

This is the project's deliberate, isolated AI seam. The deterministic core
(source -> policy -> templates -> state -> approval) decides *who* gets billed,
*how much*, and *which stage* with plain, tested code — no model anywhere near
it. This module may rephrase the already-rendered body copy and nothing else.

Three guarantees keep it safe:

  1. **Off by default.** ``NoOpToneRewriter`` (a pure identity) is the default, so
     unless tone-rewrite is explicitly enabled the whole system is deterministic
     and every existing test behaves identically.
  2. **Send-time + cached.** Rewriting happens at send-time (never in dry-run, so
     previews stay byte-identical) and is cached per (invoice, stage) keyed on the
     source-body hash. A retry/re-approve therefore sends identical bytes and the
     audit records the hash of what was actually delivered.
  3. **Fact-preservation guard.** After the model returns, ``preserves_invoice_facts``
     verifies the rewrite still contains the invoice id and the money amount. If
     the model dropped a fact, we fall back to the deterministic body — so the LLM
     can never cause a send that's missing the invoice number or amount.

The Anthropic client is injectable, so tests (and dry-runs) never hit the network,
and importing this module never requires the ``anthropic`` package or an API key —
only constructing+using ``ClaudeToneRewriter`` with the feature ON does.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from reminders.config import ToneRewriteConfig
from reminders.models import Reminder, compute_message_hash

# Tone-only rephrase. The model is told to preserve every fact and emit ONLY the
# body; the guard below mechanically enforces the money-critical ones regardless.
_SYSTEM = """You rewrite the wording of an overdue-invoice reminder email so it \
matches a requested tone. You change ONLY phrasing and tone.

Hard rules:
- Preserve every fact exactly: the invoice number, the amount and currency, the \
due date, the number of days overdue, and the payment-contact line. Do not add, \
remove, round, or alter any number, date, name, or contact detail.
- Keep it a plain-text email body. Do not add or change a subject line.
- Output ONLY the rewritten email body — no preamble, no commentary, no quoting."""


class ToneRewriter(ABC):
    """Rephrase a rendered reminder body in a given tone. Body text in, body text out."""

    @abstractmethod
    def rewrite(self, body: str, *, tone: str) -> str:
        raise NotImplementedError


class NoOpToneRewriter(ToneRewriter):
    """The default: identity. With this in place the system is fully deterministic."""

    def rewrite(self, body: str, *, tone: str) -> str:
        return body


class ClaudeToneRewriter(ToneRewriter):
    """Rephrase via the Anthropic API. Constructed only when the feature is ON.

    The ``anthropic`` package is imported lazily (so it's an optional dependency)
    and the client is injectable so tests run offline.
    """

    def __init__(self, config: ToneRewriteConfig, *, client=None):
        self.config = config
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            import anthropic  # lazy: only needed when the feature is actually used

            self._client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY from env
        return self._client

    def rewrite(self, body: str, *, tone: str) -> str:
        client = self._ensure_client()
        message = client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            thinking={"type": "disabled"},  # a short rephrase needs no reasoning
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Requested tone: {tone}\n\n"
                           f"Rewrite this overdue-invoice reminder body in that tone, "
                           f"preserving every fact exactly:\n\n{body}",
            }],
        )
        return "".join(b.text for b in message.content if b.type == "text").strip()


def preserves_invoice_facts(
    rewritten: str,
    *,
    original_body: str,
    invoice_id: str,
    amount_display: str,
) -> bool:
    """True iff the rewrite is safe to send: non-empty, still names the invoice id
    and the money amount, and isn't wildly off-length. Conservative on purpose —
    a False here means we send the deterministic body instead."""
    if not rewritten or not rewritten.strip():
        return False
    if invoice_id not in rewritten:
        return False
    if amount_display not in rewritten:
        return False
    # Degenerate-output guard: a tone rewrite shouldn't massively grow or shrink.
    length = len(rewritten)
    if length < 0.4 * len(original_body) or length > 2.5 * len(original_body):
        return False
    return True


def _amount_display(reminder: Reminder) -> str:
    # Matches TemplateEngine's amount_display so the guard checks the exact token
    # that appears in the rendered body.
    return f"{reminder.currency} {reminder.amount:,.2f}"


def apply_tone_rewrite(reminder: Reminder, *, rewriter: ToneRewriter, store) -> Reminder:
    """Return ``reminder`` with its body tone-rewritten, or unchanged.

    NoOp short-circuits (no cache touched). Otherwise: serve from the per-(invoice,
    stage) cache if present; else call the rewriter, run the fact guard (falling
    back to the deterministic body if it trips), cache the chosen body, and return
    a reminder carrying that body with a recomputed message hash.
    """
    if isinstance(rewriter, NoOpToneRewriter):
        return reminder

    cached = store.get_cached_rewrite(
        reminder.invoice_id, reminder.stage, source_hash=reminder.message_hash
    )
    if cached is not None:
        new_body = cached
    else:
        candidate = rewriter.rewrite(reminder.body, tone=reminder.tone)
        if preserves_invoice_facts(
            candidate,
            original_body=reminder.body,
            invoice_id=reminder.invoice_id,
            amount_display=_amount_display(reminder),
        ):
            new_body = candidate
        else:
            new_body = reminder.body  # guard tripped -> deterministic fallback
        store.cache_rewrite(
            reminder.invoice_id,
            reminder.stage,
            source_hash=reminder.message_hash,
            rewritten_body=new_body,
            rewritten_hash=compute_message_hash(new_body),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    if new_body == reminder.body:
        return reminder
    return reminder.model_copy(
        update={"body": new_body, "message_hash": compute_message_hash(new_body)}
    )
