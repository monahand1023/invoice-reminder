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
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from reminders.approval import ApprovalQueue, Batch
from reminders.config import Config, load_config
from reminders.models import Reminder, SendResult
from reminders.notifiers.base import Notifier
from reminders.notifiers.console import ConsoleNotifier
from reminders.notifiers.smtp import SMTPNotifier
from reminders.pipeline import ReminderPipeline
from reminders.policy import DunningPolicy
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


def build_tone_rewriter(config: Config) -> ToneRewriter:
    """The deterministic NoOp unless tone-rewrite is explicitly enabled in config."""
    if config.tone_rewrite.enabled:
        return ClaudeToneRewriter(config.tone_rewrite)
    return NoOpToneRewriter()


def open_store(config: Config) -> ReminderStateStore:
    return ReminderStateStore(config.state.db_path)


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


# --------------------------------------------------------------------------
# Command implementations (print + return exit code)
# --------------------------------------------------------------------------

def cmd_list_due(config: Config, *, as_of: date, out: TextIO) -> int:
    reminders = due_reminders(config, as_of)
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


def cmd_run(config: Config, *, as_of: date, send: bool, env: Mapping[str, str], out: TextIO) -> int:
    if not send:
        # DRY-RUN (default): render + print via console, record nothing, send nothing.
        reminders = due_reminders(config, as_of)
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
        _print(out,
               "REFUSED: `run --send` requires the seatbelt environment variable.",
               f"  Set {ALLOW_SEND_ENV}=1 to stage a real send batch.",
               "  (Even then, nothing leaves until you run `approve <batch-id>`.)")
        return 2

    batch = stage_batch(config, as_of)
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
                notifier: Notifier | None = None) -> int:
    # SEATBELT #2 again — approving is the moment real email would go out.
    if env.get(ALLOW_SEND_ENV) != "1":
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
        _print(out, f"ERROR approving batch {batch_id}: {exc}")
        return 1

    _print(out, f"Approved batch {batch_id}. Sent {len(results)} reminder(s):")
    for r in results:
        _print(out, f"  - {r.invoice_id:<10} {r.stage:<9} via {r.channel}  [{r.detail}]")
    if not results:
        _print(out, "  (nothing sent — all stages in this batch were already sent)")
    return 0


def cmd_history(config: Config, *, invoice_id: str | None, out: TextIO) -> int:
    store = open_store(config)
    try:
        rows = store.history(invoice_id=invoice_id)
    finally:
        store.close()
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
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-due", help="show invoices due for a reminder now (read-only)")

    run_p = sub.add_parser("run", help="render reminders (dry-run by default)")
    mode = run_p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="render & print via console; send/record nothing (DEFAULT)")
    mode.add_argument("--send", action="store_true",
                      help="stage a real send batch for approval (needs REMINDERS_ALLOW_SEND=1)")

    approve_p = sub.add_parser("approve", help="release an approved batch to the SMTP notifier")
    approve_p.add_argument("batch_id", help="the batch-id printed by `run --send`")

    hist_p = sub.add_parser("history", help="dump the audit trail")
    hist_p.add_argument("--invoice", help="filter to a single invoice id")

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
        return cmd_list_due(config, as_of=as_of, out=out)
    if args.command == "run":
        return cmd_run(config, as_of=as_of, send=bool(args.send), env=env, out=out)
    if args.command == "approve":
        return cmd_approve(config, batch_id=args.batch_id, env=env, out=out)
    if args.command == "history":
        return cmd_history(config, invoice_id=args.invoice, out=out)
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
