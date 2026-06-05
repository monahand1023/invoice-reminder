"""ToneRewriter — the optional, isolated LLM seam.

The LLM may ONLY rephrase body copy; it never sees or decides amounts, stages,
or recipients. These tests pin the safety properties that make that true:
  - NoOp (the default) is a pure identity — the system stays deterministic.
  - A fact-preservation guard rejects any rewrite that drops the invoice id or
    amount, falling back to the deterministic body.
  - Rewrites are cached per (invoice, stage) keyed on the source-body hash, so a
    re-send is byte-identical (idempotency/audit stay intact).
  - The Anthropic client is injectable, so none of this touches the network.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from reminders.cli import approve_batch, stage_batch
from reminders.config import ToneRewriteConfig, load_config
from reminders.models import Reminder, SendResult, compute_message_hash
from reminders.notifiers.base import Notifier
from reminders.state import ReminderStateStore
from reminders.tone import (
    ClaudeToneRewriter,
    NoOpToneRewriter,
    ToneRewriter,
    apply_tone_rewrite,
    preserves_invoice_facts,
)

GOOD_BODY = (
    "Hello Acme,\n\nInvoice INV-1003 for USD 3,200.00 was due 2026-06-04 and is "
    "now 3 days overdue. Please remit payment. Questions? Contact billing@acme.example.\n"
)


def make_reminder(body=GOOD_BODY, *, invoice_id="INV-1003", amount="3200.00",
                  currency="USD", stage="friendly", tone="friendly"):
    return Reminder(
        invoice_id=invoice_id, to_email="ap@acme.example", customer_name="Acme",
        amount=Decimal(amount), currency=currency, stage=stage, tone=tone,
        subject="Payment reminder", body=body,
        message_hash=compute_message_hash(body), days_overdue=3,
    )


# --- fakes (no network) -----------------------------------------------------

class FakeRewriter(ToneRewriter):
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def rewrite(self, body, *, tone):
        self.calls += 1
        return self.output


class _FakeMessages:
    def __init__(self, text):
        self._text = text
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)])


class FakeAnthropic:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


# --- NoOp is the deterministic default --------------------------------------

def test_noop_returns_body_unchanged():
    assert NoOpToneRewriter().rewrite("anything", tone="firm") == "anything"


def test_noop_apply_is_identity_and_writes_no_cache(tmp_path):
    store = ReminderStateStore(str(tmp_path / "s.sqlite3"))
    try:
        r = make_reminder()
        out = apply_tone_rewrite(r, rewriter=NoOpToneRewriter(), store=store)
        assert out is r  # untouched
        assert store.get_cached_rewrite(r.invoice_id, r.stage, source_hash=r.message_hash) is None
    finally:
        store.close()


# --- the fact-preservation guard --------------------------------------------

def test_guard_true_when_id_and_amount_present():
    assert preserves_invoice_facts(
        GOOD_BODY, original_body=GOOD_BODY, invoice_id="INV-1003", amount_display="USD 3,200.00"
    )


def test_guard_false_when_invoice_id_dropped():
    rewritten = GOOD_BODY.replace("INV-1003", "your invoice")
    assert not preserves_invoice_facts(
        rewritten, original_body=GOOD_BODY, invoice_id="INV-1003", amount_display="USD 3,200.00"
    )


def test_guard_false_when_amount_dropped():
    rewritten = GOOD_BODY.replace("USD 3,200.00", "the balance")
    assert not preserves_invoice_facts(
        rewritten, original_body=GOOD_BODY, invoice_id="INV-1003", amount_display="USD 3,200.00"
    )


def test_guard_false_when_empty():
    assert not preserves_invoice_facts(
        "   ", original_body=GOOD_BODY, invoice_id="INV-1003", amount_display="USD 3,200.00"
    )


# --- ClaudeToneRewriter drives an injected client (offline) -----------------

def test_claude_rewriter_uses_injected_client_and_strips_output():
    client = FakeAnthropic("  REWRITTEN: INV-1003 USD 3,200.00 still due.  \n")
    rw = ClaudeToneRewriter(ToneRewriteConfig(enabled=True, model="claude-opus-4-8"), client=client)
    out = rw.rewrite("orig", tone="firm")
    assert out == "REWRITTEN: INV-1003 USD 3,200.00 still due."
    assert client.messages.kwargs["model"] == "claude-opus-4-8"  # config-driven model


# --- apply_tone_rewrite: rewrite, guard, cache ------------------------------

def test_apply_rewrite_changes_body_and_recomputes_hash(tmp_path):
    store = ReminderStateStore(str(tmp_path / "s.sqlite3"))
    try:
        r = make_reminder()
        new_text = "Friendly note: INV-1003 (USD 3,200.00) is overdue — thanks! billing@acme.example"
        out = apply_tone_rewrite(r, rewriter=FakeRewriter(new_text), store=store)
        assert out.body == new_text
        assert out.message_hash == compute_message_hash(new_text)
        assert out.message_hash != r.message_hash
    finally:
        store.close()


def test_apply_rewrite_caches_and_does_not_call_llm_twice(tmp_path):
    store = ReminderStateStore(str(tmp_path / "s.sqlite3"))
    try:
        r = make_reminder()
        new_text = (
            "Quick reminder, Acme: invoice INV-1003 for USD 3,200.00 was due "
            "2026-06-04 and is now 3 days overdue. Please send payment when you "
            "can. Questions? Contact billing@acme.example."
        )
        fake = FakeRewriter(new_text)
        first = apply_tone_rewrite(r, rewriter=fake, store=store)
        second = apply_tone_rewrite(r, rewriter=fake, store=store)
        assert fake.calls == 1                      # second hit the cache
        assert first.body == second.body == new_text
    finally:
        store.close()


def test_apply_rewrite_falls_back_to_deterministic_body_when_guard_trips(tmp_path):
    store = ReminderStateStore(str(tmp_path / "s.sqlite3"))
    try:
        r = make_reminder()
        # LLM "loses" the invoice id — must NOT be sent.
        bad = "Hey, your balance of USD 3,200.00 is overdue. billing@acme.example"
        out = apply_tone_rewrite(r, rewriter=FakeRewriter(bad), store=store)
        assert out.body == r.body                   # deterministic fallback
        assert out.message_hash == r.message_hash
    finally:
        store.close()


# --- end-to-end: send-time rewrite + audit records what was actually sent ----

class _RecordingNotifier(Notifier):
    channel = "email"

    def __init__(self):
        self.sent = []

    def send(self, reminder) -> SendResult:
        self.sent.append(reminder)
        return SendResult(
            invoice_id=reminder.invoice_id, stage=reminder.stage, channel=self.channel,
            success=True, detail="fake", message_hash=reminder.message_hash,
            sent_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        )


class _PrefixRewriter(ToneRewriter):
    """Fact-preserving rewrite (keeps the whole body, prepends a tone marker)."""

    def rewrite(self, body, *, tone):
        return f"[tone:{tone}]\n{body}"


def test_approve_applies_rewrite_at_send_time_and_audits_the_sent_hash(config_path):
    config = load_config(config_path)
    batch = stage_batch(config, date(2026, 6, 5))
    original_by_id = {r.invoice_id: r for r in batch.reminders}
    assert original_by_id  # the mock fixture produces a non-empty batch

    notifier = _RecordingNotifier()
    approve_batch(config, batch.batch_id, notifier=notifier, rewriter=_PrefixRewriter())

    # Every delivered email used the rewritten body...
    assert all(r.body.startswith("[tone:") for r in notifier.sent)

    # ...and the audit trail stored the hash of what was ACTUALLY sent (rewritten),
    # not the original rendered body.
    store = ReminderStateStore(config.state.db_path)
    try:
        records = {rec.invoice_id: rec for rec in store.history()}
    finally:
        store.close()
    for inv_id, rec in records.items():
        sent = next(r for r in notifier.sent if r.invoice_id == inv_id)
        assert rec.message_hash == sent.message_hash
        assert rec.message_hash != original_by_id[inv_id].message_hash
