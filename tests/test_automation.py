"""Unattended-send safety core (pure decision functions).

The crux: the irreversible slice — `final` notices, high-value, and first-ever
contact to a new advertiser — is gated in CODE, so no config edit can route it
into the auto-send lane. These tests pin that invariant plus the refuse-guards.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from reminders.automation import (
    HARD_MAX_AUTO_AMOUNT,
    effective_max_auto_amount,
    hold_reason,
    partition,
    quarantine_invoices,
    refuse_if_below_floor,
    refuse_if_disabled,
    refuse_if_no_cap,
    refuse_if_over_cap,
    refuse_if_stale,
)
from reminders.config import (
    AutomationConfig, Config, DunningConfig, SenderConfig, SmtpConfig, SourceConfig,
    Stage, StateConfig,
)
from reminders.models import Invoice, Reminder


def make_config(*, cap="friendly", **automation):
    return Config(
        sender=SenderConfig(name="x", email="x@x.example", reply_to="x@x.example",
                            payment_contact="x"),
        source=SourceConfig(kind="csv", csv_path="x.csv"),
        dunning=DunningConfig(first_contact_stage_cap=cap, stages=[
            Stage(name="friendly", min_days_overdue=1, tone="friendly", template="friendly.txt.j2"),
            Stage(name="firm", min_days_overdue=14, tone="firm", template="firm.txt.j2"),
            Stage(name="final", min_days_overdue=30, tone="final", template="final.txt.j2"),
        ]),
        state=StateConfig(db_path="x.sqlite3"),
        smtp=SmtpConfig(),
        automation=AutomationConfig(**automation),
    )


def rem(stage="friendly", amount="500.00", email="ap@known.example", invoice_id="INV-1"):
    return Reminder(invoice_id=invoice_id, to_email=email, customer_name="Acme",
                    amount=Decimal(amount), currency="USD", stage=stage, tone=stage,
                    subject="s", body="b", message_hash="h", days_overdue=3)


def inv(amount="500.00", email="ap@known.example", invoice_id="INV-1"):
    return Invoice(invoice_id=invoice_id, customer_name="Acme", customer_email=email,
                   amount=Decimal(amount), currency="USD", issue_date="2026-05-01",
                   due_date="2026-06-01")


# --- effective ceiling: config can lower, never raise above the code ceiling ---

def test_effective_ceiling_is_min_of_config_and_code():
    assert effective_max_auto_amount(make_config(max_auto_amount=Decimal("1000"))) == Decimal("1000")
    assert effective_max_auto_amount(make_config(max_auto_amount=Decimal("999999"))) == HARD_MAX_AUTO_AMOUNT


# --- the auto-vs-hold decision -------------------------------------------------

def test_routine_reminder_is_auto_sendable():
    c = make_config()  # auto_stages defaults to friendly+firm, max_auto 1000
    assert hold_reason(rem("friendly", "500.00"), config=c, known_recipient=True) is None
    assert hold_reason(rem("firm", "900.00"), config=c, known_recipient=True) is None


def test_final_is_always_held_even_if_config_lists_it():
    # The code invariant: config CANNOT route a final notice into the auto lane.
    c = make_config(auto_stages=["friendly", "firm", "final"], max_auto_amount=Decimal("999999"))
    assert hold_reason(rem("final", "500.00"), config=c, known_recipient=True) == "final-stage"


def test_high_value_is_always_held_even_if_config_raises_the_amount():
    c = make_config(auto_stages=["friendly", "firm"], max_auto_amount=Decimal("999999"))
    # over the HARD code ceiling -> held regardless of the inflated config value
    assert hold_reason(rem("firm", "5000.00"), config=c, known_recipient=True) == "amount-over-ceiling"


def test_first_contact_to_new_advertiser_is_held():
    c = make_config()
    assert hold_reason(rem("friendly", "500.00"), config=c, known_recipient=False) == "new-advertiser"


def test_config_amount_ceiling_holds_below_the_hard_ceiling():
    c = make_config(max_auto_amount=Decimal("1000"))
    assert hold_reason(rem("firm", "1500.00"), config=c, known_recipient=True) == "amount-over-ceiling"


def test_partition_splits_auto_and_held():
    c = make_config()
    reminders = [
        rem("friendly", "500.00", invoice_id="A"),                    # auto
        rem("final", "500.00", invoice_id="B"),                       # held: final
        rem("firm", "9000.00", invoice_id="C"),                       # held: amount
    ]
    auto, held = partition(reminders, config=c, known_recipients={"ap@known.example"})
    assert [r.invoice_id for r in auto] == ["A"]
    assert {r.invoice_id for r, _ in held} == {"B", "C"}


# --- ingest quarantine (amount band + email) ----------------------------------

def test_quarantine_nonpositive_and_out_of_band_amounts():
    c = make_config(min_amount=Decimal("1"), max_amount=Decimal("150000"))
    clean, bad = quarantine_invoices([
        inv("500.00", invoice_id="OK"),
        inv("0.00", invoice_id="ZERO"),
        inv("-500.00", invoice_id="CREDIT"),
        inv("200000.00", invoice_id="HUGE"),
    ], config=c)
    assert [i.invoice_id for i in clean] == ["OK"]
    assert {q[0] for q in bad} == {"ZERO", "CREDIT", "HUGE"}


def test_quarantine_invalid_email():
    c = make_config()
    clean, bad = quarantine_invoices([
        inv(email="ap@good.example", invoice_id="OK"),
        inv(email="not-an-email", invoice_id="BAD"),
        inv(email="", invoice_id="EMPTY"),
    ], config=c)
    assert [i.invoice_id for i in clean] == ["OK"]
    assert {q[0] for q in bad} == {"BAD", "EMPTY"}


# --- refuse-guards (fail closed) ----------------------------------------------

def test_disabled_or_hold_refuses():
    assert refuse_if_disabled(make_config(enabled=False), hold_exists=False) is not None
    assert refuse_if_disabled(make_config(enabled=True), hold_exists=True) is not None
    assert refuse_if_disabled(make_config(enabled=True), hold_exists=False) is None


def test_stale_csv_refuses():
    now = datetime(2026, 6, 5, 8, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(hours=2)
    stale = now - timedelta(hours=72)
    assert refuse_if_stale(fresh, max_age_hours=26.0, now=now) is None
    assert refuse_if_stale(stale, max_age_hours=26.0, now=now) is not None


def test_missing_cap_refuses():
    assert refuse_if_no_cap(make_config(cap=None)) is not None
    assert refuse_if_no_cap(make_config(cap="friendly")) is None


def test_volume_floor_and_per_run_cap_refuse():
    c = make_config(min_open_invoices=1, max_send_per_run=25)
    assert refuse_if_below_floor(0, config=c) is not None
    assert refuse_if_below_floor(5, config=c) is None
    assert refuse_if_over_cap(26, config=c) is not None
    assert refuse_if_over_cap(25, config=c) is None
