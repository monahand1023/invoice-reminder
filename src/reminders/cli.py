"""Command-line interface (argparse — Typer not required).

Commands:
    list-due                 read-only; what would be sent right now, and at which stage
    run [--dry-run|--send]   dry-run (DEFAULT) prints via console & records nothing;
                             --send enqueues a batch to the ApprovalQueue (sends nothing)
    approve <batch-id>       releases an approved batch to the SMTPNotifier; records sends
    history [--invoice ID]   dump the audit trail

SAFETY (two seatbelts, both required for a real send):
    1. You must explicitly pass --send, and later run `approve`.
    2. The environment variable REMINDERS_ALLOW_SEND=1 must be set.
Dry-run is the default everywhere. Nothing leaves the building without both.

The command bodies are split into thin print-wrappers (cmd_*) and pure helpers
(build_*, stage_batch, approve_batch, ...) so tests can drive the real flow with
an injected notifier instead of hitting SMTP.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from reminders.approval import ApprovalError, ApprovalQueue, Batch
from reminders.config import Config, load_config
from reminders.models import Reminder, SendResult
from reminders.notifiers.base import Notifier
from reminders.notifiers.console import ConsoleNotifier
from reminders.notifiers.smtp import SMTPNotifier, send_operator_email
from reminders.pipeline import ReminderPipeline
from reminders.policy import DunningPolicy
from reminders.sources.base import InvoiceSource
from reminders.sources.mock import MockInvoiceSource
from reminders.state import AlreadySentError, ReminderStateStore
from reminders.templates import TemplateEngine
from reminders.tone import (
    ClaudeToneRewriter,
    NoOpToneRewriter,
    ToneRewriter,
    apply_tone_rewrite,
)

ALLOW_SEND_ENV = "REMINDERS_ALLOW_SEND"
_DEFAULT_CONFIG_CANDIDATES = ("config.yaml", "config.example.yaml")


# --------------------------------------------------------------------------
# Component wiring (everything reads from Config; nothing is hardcoded)
# --------------------------------------------------------------------------

def build_source(config: Config):
    kind = config.source.kind
    if kind == "mock":
        if not config.source.fixture_path:
            raise ValueError("source.kind=mock requires source.fixture_path")
        return MockInvoiceSource(config.source.fixture_path)
    if kind == "csv":
        if not config.source.csv_path:
            raise ValueError("source.kind=csv requires source.csv_path")
        from reminders.sources.csv_source import CsvInvoiceSource
        return CsvInvoiceSource(config.source.csv_path)
    # QBO/QBD are built but unverified against a live tenant. They construct fine;
    # list_open_invoices() raises a clear error if credentials / a poll cycle are
    # missing (they never pretend to have data).
    if kind == "quickbooks_online":
        from reminders.sources.quickbooks_online import QuickBooksOnlineSource
        return QuickBooksOnlineSource(config.source.quickbooks_online)
    if kind == "quickbooks_desktop":
        from reminders.sources.quickbooks_desktop import QuickBooksDesktopSource
        return QuickBooksDesktopSource(config.source.quickbooks_desktop)
    # Magazine Manager has no public API — this stays a stub (no invented endpoints).
    if kind == "magazine_manager":
        from reminders.sources.magazine_manager import MagazineManagerSource
        return MagazineManagerSource()
    raise ValueError(f"unknown source.kind: {kind!r}")


def build_pipeline(config: Config, store: ReminderStateStore) -> ReminderPipeline:
    return ReminderPipeline(
        source=build_source(config),
        policy=DunningPolicy(
            config.dunning.stages,
            first_contact_stage_cap=config.dunning.first_contact_stage_cap,
        ),
        templates=TemplateEngine(config.templates_dir, config.sender),
        store=store,
    )


class _ListInvoiceSource(InvoiceSource):
    """In-memory source over an already-validated invoice list (for cron-run)."""

    def __init__(self, invoices: list):
        self._invoices = list(invoices)

    def list_open_invoices(self) -> list:
        return list(self._invoices)


def build_tone_rewriter(config: Config) -> ToneRewriter:
    """The deterministic NoOp unless tone-rewrite is explicitly enabled in config."""
    if config.tone_rewrite.enabled:
        return ClaudeToneRewriter(config.tone_rewrite)
    return NoOpToneRewriter()


def open_store(config: Config) -> ReminderStateStore:
    return ReminderStateStore(config.state.db_path)


def cold_start_refusal(config: Config, *, allow_cold_start: bool) -> str | None:
    """Return a refusal message if staging a send now would be a risky cold start.

    Risky = a REAL source (not the mock demo) + an EMPTY audit trail (nothing ever
    sent) + NO first_contact_stage_cap configured. In that exact situation the very
    first unattended run would open a long-overdue backlog with FINAL notices. We
    enforce the cap-or-acknowledge decision in code so it can't be lost by copying
    the demo config (which ships the cap off). Returns None when it's safe."""
    if allow_cold_start or config.source.kind == "mock":
        return None
    if config.dunning.first_contact_stage_cap is not None:
        return None
    store = open_store(config)
    try:
        if store.has_any_sends():
            return None
    finally:
        store.close()
    return (
        "REFUSED (cold start): this is the first send against a real source and no "
        "`dunning.first_contact_stage_cap` is set, so a long-overdue backlog would "
        "open with FINAL notices. Set `first_contact_stage_cap: \"friendly\"` in "
        "config.yaml (recommended), or pass --allow-cold-start to proceed anyway."
    )


def open_queue(config: Config) -> ApprovalQueue:
    return ApprovalQueue(config.state.db_path)


# --------------------------------------------------------------------------
# Pure-ish helpers (used by both the CLI and the tests)
# --------------------------------------------------------------------------

def due_reminders(config: Config, as_of: date) -> list[Reminder]:
    store = open_store(config)
    try:
        return build_pipeline(config, store).due_reminders(as_of)
    finally:
        store.close()


def stage_batch(config: Config, as_of: date, *, batch_id: str | None = None,
                created_at: datetime | None = None) -> Batch:
    """Build the due reminders and park them in the ApprovalQueue as 'pending'.
    Sends NOTHING. Returns the enqueued batch."""
    batch_id = batch_id or f"B-{uuid.uuid4().hex[:12]}"
    created_at = created_at or datetime.now(timezone.utc)
    store = open_store(config)
    try:
        reminders = build_pipeline(config, store).due_reminders(as_of)
    finally:
        store.close()
    queue = open_queue(config)
    try:
        queue.enqueue(reminders, batch_id=batch_id, created_at=created_at)
        return queue.get_batch(batch_id)
    finally:
        queue.close()


def approve_batch(
    config: Config,
    batch_id: str,
    *,
    notifier: Notifier,
    rewriter: ToneRewriter | None = None,
) -> list[SendResult]:
    """Release a batch to the notifier, recording each send. Re-runnable and
    idempotent: a stage already in the state store is skipped, never re-sent.

    If a tone-rewriter is supplied (and enabled), each reminder's body is rephrased
    at this point — never in dry-run — with the result cached so retries are
    byte-identical and the audit records the hash of what was actually sent."""
    rewriter = rewriter or NoOpToneRewriter()
    queue = open_queue(config)
    store = open_store(config)
    results: list[SendResult] = []
    try:
        batch = queue.get_batch(batch_id)
        if batch.status == "pending":
            queue.approve(batch_id)        # pending -> approved
        elif batch.status == "sent":
            return results                  # nothing to do
        elif batch.status == "canceled":
            raise ApprovalError(f"batch {batch_id} was canceled; refusing to send")
        # status 'approved' (incl. a resumed/interrupted run) falls through

        for reminder in batch.reminders:
            if store.already_sent(reminder.invoice_id, reminder.stage):
                continue                    # idempotency: never send twice
            reminder = apply_tone_rewrite(reminder, rewriter=rewriter, store=store)
            result = notifier.send(reminder)
            if result.success:
                try:
                    store.record_send(result, to_email=reminder.to_email, batch_id=batch_id)
                    results.append(result)
                except AlreadySentError:
                    # Backstop race: another run recorded it first. Do not re-send.
                    continue
        queue.mark_sent(batch_id)
        return results
    finally:
        store.close()
        queue.close()


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def _money(amount: Decimal, currency: str) -> str:
    return f"{currency} {amount:,.2f}"


def _print(out: TextIO, *lines: str) -> None:
    for line in lines:
        out.write(line + "\n")


def _emit_json(out: TextIO, obj) -> None:
    """Write a single JSON object (the whole stdout) for scripting / monitoring."""
    out.write(json.dumps(obj) + "\n")


def _reminder_json(r: Reminder) -> dict:
    # amount is Decimal -> serialize as a string to stay exact.
    return {
        "invoice_id": r.invoice_id,
        "stage": r.stage,
        "tone": r.tone,
        "days_overdue": r.days_overdue,
        "amount": str(r.amount),
        "currency": r.currency,
        "customer_name": r.customer_name,
        "to_email": r.to_email,
    }


# --------------------------------------------------------------------------
# Command implementations (print + return exit code)
# --------------------------------------------------------------------------

def cmd_list_due(config: Config, *, as_of: date, out: TextIO, as_json: bool = False) -> int:
    reminders = due_reminders(config, as_of)
    if as_json:
        _emit_json(out, {"as_of": as_of.isoformat(), "count": len(reminders),
                         "reminders": [_reminder_json(r) for r in reminders]})
        return 0
    _print(out, f"Invoices due for a reminder as of {as_of.isoformat()}:")
    if not reminders:
        _print(out, "  (none)")
        return 0
    _print(out, f"  {'INVOICE':<10} {'STAGE':<9} {'OVERDUE':>7}  {'AMOUNT':>16}  CUSTOMER")
    for r in reminders:
        _print(
            out,
            f"  {r.invoice_id:<10} {r.stage:<9} {r.days_overdue:>5}d  "
            f"{_money(r.amount, r.currency):>16}  {r.customer_name}",
        )
    by_stage: dict[str, int] = {}
    for r in reminders:
        by_stage[r.stage] = by_stage.get(r.stage, 0) + 1
    summary = ", ".join(f"{n} {s}" for s, n in by_stage.items())
    _print(out, f"Total: {len(reminders)} reminder(s) due ({summary}).")
    return 0


def cmd_run(config: Config, *, as_of: date, send: bool, env: Mapping[str, str], out: TextIO,
            as_json: bool = False, allow_cold_start: bool = False) -> int:
    if not send:
        # DRY-RUN (default): render + print via console, record nothing, send nothing.
        reminders = due_reminders(config, as_of)
        if as_json:
            _emit_json(out, {"mode": "dry-run", "as_of": as_of.isoformat(),
                             "count": len(reminders),
                             "reminders": [_reminder_json(r) for r in reminders]})
            return 0
        _print(out,
               f"DRY-RUN — as of {as_of.isoformat()}. Nothing is sent; nothing is recorded.",
               f"{len(reminders)} reminder(s) would be sent:\n")
        console = ConsoleNotifier(stream=out)
        for r in reminders:
            console.send(r)
        _print(out, f"\nDRY-RUN complete. {len(reminders)} reminder(s) previewed. "
                    f"State store untouched.")
        return 0

    # --send: SEATBELT #2 — refuse unless REMINDERS_ALLOW_SEND=1.
    if env.get(ALLOW_SEND_ENV) != "1":
        if as_json:
            _emit_json(out, {"error": "seatbelt_required",
                             "message": f"{ALLOW_SEND_ENV}=1 required to stage a send batch"})
            return 2
        _print(out,
               "REFUSED: `run --send` requires the seatbelt environment variable.",
               f"  Set {ALLOW_SEND_ENV}=1 to stage a real send batch.",
               "  (Even then, nothing leaves until you run `approve <batch-id>`.)")
        return 2

    # Cold-start money-safety: don't let the first unattended real run open a
    # backlog with FINAL notices (see cold_start_refusal).
    refusal = cold_start_refusal(config, allow_cold_start=allow_cold_start)
    if refusal is not None:
        if as_json:
            _emit_json(out, {"error": "cold_start_unsafe", "message": refusal})
            return 2
        _print(out, refusal)
        return 2

    batch = stage_batch(config, as_of)
    if as_json:
        _emit_json(out, {"batch_id": batch.batch_id, "as_of": as_of.isoformat(),
                         "count": len(batch.reminders),
                         "reminders": [_reminder_json(r) for r in batch.reminders]})
        return 0
    _print(out,
           f"Staged a send batch (NOTHING SENT YET) as of {as_of.isoformat()}.",
           f"  batch-id : {batch.batch_id}",
           f"  reminders: {len(batch.reminders)}")
    for r in batch.reminders:
        _print(out, f"    - {r.invoice_id:<10} {r.stage:<9} {_money(r.amount, r.currency):>16}"
                    f"  -> {r.to_email}")
    _print(out,
           "",
           ">>> Human approval required. Review the batch above, then run:",
           f"      reminders approve {batch.batch_id}")
    return 0


def cmd_approve(config: Config, *, batch_id: str, env: Mapping[str, str], out: TextIO,
                notifier: Notifier | None = None, as_json: bool = False) -> int:
    # SEATBELT #2 again — approving is the moment real email would go out.
    if env.get(ALLOW_SEND_ENV) != "1":
        if as_json:
            _emit_json(out, {"error": "seatbelt_required", "batch_id": batch_id,
                             "message": f"{ALLOW_SEND_ENV}=1 required to approve a batch"})
            return 2
        _print(out,
               "REFUSED: approving a batch sends real email and requires the seatbelt.",
               f"  Set {ALLOW_SEND_ENV}=1 to allow sending.")
        return 2

    if notifier is None:
        notifier = SMTPNotifier(config.smtp, allow_send=True)

    try:
        results = approve_batch(
            config, batch_id, notifier=notifier, rewriter=build_tone_rewriter(config)
        )
    except Exception as exc:  # UnknownBatchError etc. -> clear message, nonzero exit
        if as_json:
            _emit_json(out, {"error": "approve_failed", "batch_id": batch_id, "message": str(exc)})
            return 1
        _print(out, f"ERROR approving batch {batch_id}: {exc}")
        return 1

    if as_json:
        _emit_json(out, {"batch_id": batch_id, "sent": len(results),
                         "results": [{"invoice_id": r.invoice_id, "stage": r.stage,
                                      "channel": r.channel, "detail": r.detail}
                                     for r in results]})
        return 0

    _print(out, f"Approved batch {batch_id}. Sent {len(results)} reminder(s):")
    for r in results:
        _print(out, f"  - {r.invoice_id:<10} {r.stage:<9} via {r.channel}  [{r.detail}]")
    if not results:
        _print(out, "  (nothing sent — all stages in this batch were already sent)")
    return 0


def cmd_history(config: Config, *, invoice_id: str | None, out: TextIO,
                as_json: bool = False) -> int:
    store = open_store(config)
    try:
        rows = store.history(invoice_id=invoice_id)
    finally:
        store.close()
    if as_json:
        _emit_json(out, {"count": len(rows), "records": [
            {"invoice_id": r.invoice_id, "stage": r.stage, "channel": r.channel,
             "sent_at": r.sent_at, "to_email": r.to_email, "batch_id": r.batch_id,
             "message_hash": r.message_hash}
            for r in rows]})
        return 0
    scope = f" for {invoice_id}" if invoice_id else ""
    _print(out, f"Audit trail{scope} — {len(rows)} record(s):")
    if not rows:
        _print(out, "  (no sends recorded)")
        return 0
    _print(out, f"  {'INVOICE':<10} {'STAGE':<9} {'CHANNEL':<8} {'SENT_AT':<26} "
                f"{'TO':<26} {'BATCH':<16} MSG_HASH")
    for r in rows:
        _print(out,
               f"  {r.invoice_id:<10} {r.stage:<9} {r.channel:<8} {r.sent_at:<26} "
               f"{r.to_email:<26} {(r.batch_id or '-'):<16} {r.message_hash}")
    return 0


def cmd_batches(config: Config, *, cancel_id: str | None, out: TextIO,
                as_json: bool = False) -> int:
    queue = open_queue(config)
    try:
        if cancel_id is not None:
            try:
                queue.cancel(cancel_id)
            except Exception as exc:   # UnknownBatchError / ApprovalError
                if as_json:
                    _emit_json(out, {"error": "cancel_failed", "batch_id": cancel_id,
                                     "message": str(exc)})
                else:
                    _print(out, f"ERROR canceling {cancel_id}: {exc}")
                return 1
            if as_json:
                _emit_json(out, {"canceled": cancel_id})
            else:
                _print(out, f"Canceled batch {cancel_id}.")
            return 0
        summaries = queue.list_batches()
    finally:
        queue.close()

    if as_json:
        _emit_json(out, {"count": len(summaries), "batches": [
            {"batch_id": b.batch_id, "status": b.status, "created_at": b.created_at,
             "count": b.count} for b in summaries]})
        return 0
    _print(out, f"Approval batches — {len(summaries)} total:")
    if not summaries:
        _print(out, "  (none)")
        return 0
    _print(out, f"  {'BATCH':<16} {'STATUS':<10} {'COUNT':>5}  CREATED")
    for b in summaries:
        _print(out, f"  {b.batch_id:<16} {b.status:<10} {b.count:>5}  {b.created_at}")
    pending = sum(1 for b in summaries if b.status == "pending")
    if pending:
        _print(out, f"\n{pending} pending batch(es). Approve with `approve <batch-id>` "
                    f"or discard with `batches --cancel <batch-id>`.")
    return 0


def _render_cron_summary(s: dict) -> str:
    lines = [
        f"[reminders cron-run] {s['outcome']}",
        f"  as-of: {s['as_of']}   mode: {'DRY-RUN' if s['dry_run'] else 'LIVE'}",
        f"  auto-{'would-send' if s['dry_run'] else 'sent'}: {s['auto_count']}"
        f"   held-for-review: {s['held']}   quarantined: {s['quarantined']}",
    ]
    if s.get("held_batch_id"):
        lines.append(f"  held batch {s['held_batch_id']} -> review with "
                     f"`reminders approve {s['held_batch_id']}`")
    for inv_id, reason in s.get("held_reasons", []):
        lines.append(f"    HELD  {inv_id}: {reason}")
    for inv_id, reason in s.get("quarantine", []):
        lines.append(f"    QUARANTINE  {inv_id}: {reason}")
    return "\n".join(lines)


def _finish_cron(config: Config, out: TextIO, summary: dict, *, as_json: bool) -> None:
    """Print the summary and best-effort email it to the operator (never lets a mail
    failure change the run's exit code)."""
    if as_json:
        _emit_json(out, summary)
    else:
        _print(out, _render_cron_summary(summary))
    to = config.automation.summary_to
    if to:
        try:
            send_operator_email(config.smtp, to=to,
                                subject=f"[invoice-reminders] cron-run: {summary['outcome']}",
                                body=_render_cron_summary(summary))
        except Exception as exc:  # mail must never break the run
            _print(out, f"(warning: could not email run summary to {to}: {exc})")


def cmd_cron_run(config: Config, *, as_of: date, env: Mapping[str, str], out: TextIO,
                 notifier: Notifier | None = None, now: datetime | None = None,
                 dry_run: bool = False, as_json: bool = False) -> int:
    """Unattended send: stage -> auto-send the routine lane -> divert the irreversible
    slice to the human queue, all behind fail-closed guards. See reminders.automation."""
    from reminders import automation as A
    from reminders.sources.csv_source import CsvInvoiceSource, DataIntegrityError

    now = now or datetime.now(timezone.utc)

    def refuse(reason: str) -> int:
        _finish_cron(config, out, {
            "outcome": f"REFUSED: {reason}", "as_of": as_of.isoformat(), "dry_run": dry_run,
            "auto_count": 0, "held": 0, "held_batch_id": None, "quarantined": 0,
            "quarantine": [], "held_reasons": [],
        }, as_json=as_json)
        return 2

    # 1. authorization (fail closed). enabled + (for live) the env seatbelt + cap.
    hold_path = config.automation.hold_flag_path
    hold_exists = bool(hold_path) and Path(hold_path).exists()
    for r in (A.refuse_if_disabled(config, hold_exists=hold_exists), A.refuse_if_no_cap(config)):
        if r:
            return refuse(r)
    if not dry_run and env.get(ALLOW_SEND_ENV) != "1":
        return refuse(f"{ALLOW_SEND_ENV}=1 is also required for a live unattended send")

    # 2. freshness (csv only)
    if config.source.kind == "csv" and config.source.csv_path:
        try:
            mtime = datetime.fromtimestamp(
                Path(config.source.csv_path).stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            return refuse(f"cannot read the CSV export: {exc}")
        r = A.refuse_if_stale(mtime, max_age_hours=config.automation.csv_max_age_hours, now=now)
        if r:
            return refuse(r)

    # 3. load + quarantine (strict CSV: missing status/dnc column aborts)
    try:
        source = (CsvInvoiceSource(config.source.csv_path, strict=True)
                  if config.source.kind == "csv" else build_source(config))
        invoices = source.list_open_invoices()
    except (DataIntegrityError, ValueError) as exc:
        return refuse(f"export integrity: {exc}")
    quarantined = list(getattr(source, "quarantined", []))
    clean, amount_q = A.quarantine_invoices(invoices, config=config)
    quarantined += amount_q

    r = A.refuse_if_below_floor(len(clean), config=config)
    if r:
        return refuse(r)

    # 4. due reminders over CLEAN invoices; which recipients are already known
    store = open_store(config)
    try:
        pipeline = ReminderPipeline(
            source=_ListInvoiceSource(clean),
            policy=DunningPolicy(config.dunning.stages,
                                 first_contact_stage_cap=config.dunning.first_contact_stage_cap),
            templates=TemplateEngine(config.templates_dir, config.sender),
            store=store)
        reminders = pipeline.due_reminders(as_of)
        known = {r.to_email for r in reminders if store.has_contacted_email(r.to_email)}
    finally:
        store.close()

    # 5. partition into auto vs human-held (code-enforced gate)
    auto, held = A.partition(reminders, config=config, known_recipients=known)

    # 6. per-run cap on the auto lane
    r = A.refuse_if_over_cap(len(auto), config=config)
    if r:
        return refuse(r)

    held_batch_id = None
    sent = 0
    if not dry_run:
        if auto:
            auto_id = f"AUTO-{uuid.uuid4().hex[:10]}"
            queue = open_queue(config)
            try:
                queue.enqueue(auto, batch_id=auto_id, created_at=now)
            finally:
                queue.close()
            send_notifier = notifier or SMTPNotifier(config.smtp, allow_send=True)
            results = approve_batch(config, auto_id, notifier=send_notifier,
                                    rewriter=build_tone_rewriter(config))
            sent = len(results)
        if held:
            held_batch_id = f"HELD-{uuid.uuid4().hex[:10]}"
            queue = open_queue(config)
            try:
                queue.enqueue([r for r, _ in held], batch_id=held_batch_id, created_at=now)
            finally:
                queue.close()

    _finish_cron(config, out, {
        "outcome": "DRY-RUN" if dry_run else "COMPLETED",
        "as_of": as_of.isoformat(), "dry_run": dry_run,
        "auto_count": len(auto) if dry_run else sent,
        "held": len(held), "held_batch_id": held_batch_id,
        "quarantined": len(quarantined), "quarantine": quarantined,
        "held_reasons": [(r.invoice_id, reason) for r, reason in held],
    }, as_json=as_json)
    return 0


# --------------------------------------------------------------------------
# argparse wiring
# --------------------------------------------------------------------------

def _resolve_config_path(explicit: str | None, out: TextIO) -> str:
    if explicit:
        return explicit
    for candidate in _DEFAULT_CONFIG_CANDIDATES:
        if Path(candidate).exists():
            if candidate != "config.yaml":
                _print(out, f"(note: config.yaml not found; using {candidate})")
            return candidate
    raise SystemExit(
        "No config found. Copy config.example.yaml to config.yaml (or pass --config)."
    )


def _parse_as_of(value: str | None) -> date:
    if value is None:
        return date.today()
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reminders",
        description="Deterministic accounts-receivable (dunning) reminders. "
                    "Dry-run is the default; real sends need --send, approval, "
                    "and REMINDERS_ALLOW_SEND=1.",
    )
    parser.add_argument("--config", help="path to config.yaml (default: config.yaml then config.example.yaml)")
    parser.add_argument("--as-of", help="evaluate as of this date (YYYY-MM-DD); default: today")
    parser.add_argument("--json", action="store_true",
                        help="emit one machine-readable JSON object on stdout (scripting / monitoring)")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list-due", help="show invoices due for a reminder now (read-only)")

    run_p = sub.add_parser("run", help="render reminders (dry-run by default)")
    mode = run_p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="render & print via console; send/record nothing (DEFAULT)")
    mode.add_argument("--send", action="store_true",
                      help="stage a real send batch for approval (needs REMINDERS_ALLOW_SEND=1)")
    run_p.add_argument("--allow-cold-start", action="store_true",
                       help="acknowledge a first real send with no first_contact_stage_cap set")

    approve_p = sub.add_parser("approve", help="release an approved batch to the SMTP notifier")
    approve_p.add_argument("batch_id", help="the batch-id printed by `run --send`")

    hist_p = sub.add_parser("history", help="dump the audit trail")
    hist_p.add_argument("--invoice", help="filter to a single invoice id")

    batches_p = sub.add_parser("batches",
                               help="list staged approval batches; --cancel to discard one")
    batches_p.add_argument("--cancel", metavar="BATCH_ID",
                           help="discard a pending/approved batch (it can never be sent)")

    cron_p = sub.add_parser("cron-run",
                            help="unattended send: auto-send the routine lane, hold the rest "
                                 "for review (needs automation.enabled)")
    cron_p.add_argument("--dry-run", action="store_true",
                        help="run all guards + partition but send nothing (canary mode)")

    # Also accept --json *after* the subcommand (e.g. `run --send --json`), the
    # natural spot for a command string. SUPPRESS keeps the global value from being
    # clobbered when the trailing flag is absent.
    for p in (list_p, run_p, approve_p, hist_p, batches_p, cron_p):
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="emit one machine-readable JSON object on stdout")

    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None,
         env: Mapping[str, str] | None = None) -> int:
    import os
    out = out if out is not None else sys.stdout
    env = env if env is not None else os.environ

    parser = build_parser()
    args = parser.parse_args(argv)

    config = load_config(_resolve_config_path(args.config, out))
    as_of = _parse_as_of(args.as_of)

    if args.command == "list-due":
        return cmd_list_due(config, as_of=as_of, out=out, as_json=args.json)
    if args.command == "run":
        return cmd_run(config, as_of=as_of, send=bool(args.send), env=env, out=out,
                       as_json=args.json,
                       allow_cold_start=getattr(args, "allow_cold_start", False))
    if args.command == "approve":
        return cmd_approve(config, batch_id=args.batch_id, env=env, out=out, as_json=args.json)
    if args.command == "history":
        return cmd_history(config, invoice_id=args.invoice, out=out, as_json=args.json)
    if args.command == "batches":
        return cmd_batches(config, cancel_id=args.cancel, out=out, as_json=args.json)
    if args.command == "cron-run":
        return cmd_cron_run(config, as_of=as_of, env=env, out=out,
                            dry_run=bool(getattr(args, "dry_run", False)), as_json=args.json)
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
