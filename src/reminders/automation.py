"""Unattended (cron) send safety — the decision core for `reminders cron-run`.

Removing the human approval keystroke means these guards ARE the safety. The
design (verified by an adversarial review): auto-send only the *routine* lane and
divert the irreversible slice to the human approval queue. Crucially, that slice —
``final`` notices, high-value balances, and first-ever contact to a new advertiser —
is gated **in code here**, so no `config.yaml` edit can ever route it into the auto
lane. Config may only make the gate *more* conservative.

This module is pure decision logic (no I/O); ``reminders.cli.cmd_cron_run`` does the
wiring and the actual send. Everything fails **closed**: a guard that trips refuses
the whole run (and the operator is alerted) rather than sending anything.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from reminders.config import Config
from reminders.models import Invoice, Reminder

# --- code-enforced floor (config cannot weaken these) -------------------------
NEVER_AUTO_STAGES = frozenset({"final"})       # final notices ALWAYS need a human
HARD_MAX_AUTO_AMOUNT = Decimal("2500")         # nothing above this auto-sends, ever

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def effective_max_auto_amount(config: Config) -> Decimal:
    """The auto-send amount ceiling: the lower of config and the hard code ceiling.
    Config can tighten it; it can never raise it above HARD_MAX_AUTO_AMOUNT."""
    return min(config.automation.max_auto_amount, HARD_MAX_AUTO_AMOUNT)


def hold_reason(reminder: Reminder, *, config: Config, known_recipient: bool) -> str | None:
    """None if this reminder may auto-send; otherwise the reason it must be held
    for human review. Checked in order so the code invariants win first."""
    if reminder.stage in NEVER_AUTO_STAGES:
        return "final-stage"
    if reminder.amount > effective_max_auto_amount(config):
        return "amount-over-ceiling"
    if reminder.stage not in config.automation.auto_stages:
        return f"stage-not-auto:{reminder.stage}"
    if not known_recipient:
        return "new-advertiser"
    return None


def partition(
    reminders: list[Reminder], *, config: Config, known_recipients: set[str]
) -> tuple[list[Reminder], list[tuple[Reminder, str]]]:
    """Split into (auto-send, held-for-human-with-reason)."""
    auto: list[Reminder] = []
    held: list[tuple[Reminder, str]] = []
    for r in reminders:
        reason = hold_reason(r, config=config, known_recipient=r.to_email in known_recipients)
        (auto if reason is None else held).append(r if reason is None else (r, reason))
    return auto, held


def quarantine_invoices(
    invoices: list[Invoice], *, config: Config
) -> tuple[list[Invoice], list[tuple[str, str]]]:
    """Drop rows unsafe to dun unattended: non-positive / out-of-band amounts and
    invalid recipient emails. Returns (clean, [(invoice_id, reason), ...])."""
    a = config.automation
    clean: list[Invoice] = []
    quarantined: list[tuple[str, str]] = []
    for i in invoices:
        if i.amount <= 0:
            quarantined.append((i.invoice_id, "non-positive-amount"))
        elif i.amount < a.min_amount:
            quarantined.append((i.invoice_id, "below-min-amount"))
        elif i.amount > a.max_amount:
            quarantined.append((i.invoice_id, "above-max-amount"))
        elif not _EMAIL_RE.match(i.customer_email or ""):
            quarantined.append((i.invoice_id, "invalid-email"))
        else:
            clean.append(i)
    return clean, quarantined


# --- refuse-guards (each returns a refusal message, or None if safe) ----------

def refuse_if_disabled(config: Config, *, hold_exists: bool) -> str | None:
    if not config.automation.enabled:
        return "automation is disabled (set automation.enabled: true to allow unattended sends)"
    if hold_exists:
        return f"HOLD flag present at {config.automation.hold_flag_path!r}; all sends paused"
    return None


def refuse_if_stale(csv_mtime: datetime, *, max_age_hours: float, now: datetime) -> str | None:
    age_hours = (now - csv_mtime).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        return (f"export is {age_hours:.0f}h old (max {max_age_hours:.0f}h) — refusing to dun "
                f"off stale data; re-export the AR/aging report")
    return None


def refuse_if_no_cap(config: Config) -> str | None:
    if config.dunning.first_contact_stage_cap is None:
        return ("unattended mode requires dunning.first_contact_stage_cap to be set "
                "(e.g. \"friendly\")")
    return None


def refuse_if_below_floor(open_count: int, *, config: Config) -> str | None:
    floor = config.automation.min_open_invoices
    if open_count < floor:
        return (f"only {open_count} open invoice(s) (floor {floor}) — treating an empty/partial "
                f"export as a failure, not a quiet 'nothing due'")
    return None


def refuse_if_over_cap(auto_count: int, *, config: Config) -> str | None:
    cap = config.automation.max_send_per_run
    if auto_count > cap:
        return (f"{auto_count} reminders would auto-send (per-run cap {cap}) — refusing the whole "
                f"batch; check the export or raise automation.max_send_per_run")
    return None
