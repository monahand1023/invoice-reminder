"""DunningPolicy — the deterministic core. Stage selection across every edge case.

The policy picks the highest stage an invoice currently *qualifies* for (its
"bucket") and returns it only if that stage has not already been sent. It must
NEVER look at amount, currency, or anything an LLM produced.
"""
from datetime import date

import pytest

from reminders.config import Stage
from reminders.models import Invoice
from reminders.policy import DunningPolicy

AS_OF = date(2026, 6, 5)

DEFAULT_STAGES = [
    Stage(name="friendly", min_days_overdue=1, tone="friendly", template="friendly.txt.j2"),
    Stage(name="firm", min_days_overdue=14, tone="firm", template="firm.txt.j2"),
    Stage(name="final", min_days_overdue=30, tone="final", template="final.txt.j2"),
]


def policy():
    return DunningPolicy(DEFAULT_STAGES)


def invoice(days_overdue, *, status="open", do_not_contact=False):
    """Build an invoice whose due_date is `days_overdue` days before AS_OF."""
    due = date.fromordinal(AS_OF.toordinal() - days_overdue)
    return Invoice(
        invoice_id="INV-X",
        customer_name="Acme",
        customer_email="ap@acme.example",
        amount="1000.00",
        currency="USD",
        issue_date="2026-01-01",
        due_date=due.isoformat(),
        status=status,
        do_not_contact=do_not_contact,
    )


def stage_name(stage):
    return stage.name if stage else None


# --- the happy-path buckets -------------------------------------------------

@pytest.mark.parametrize(
    "days,expected",
    [
        (-20, None),       # not due
        (0, None),         # due today
        (1, "friendly"),   # boundary +1
        (3, "friendly"),
        (13, "friendly"),  # just below firm
        (14, "firm"),      # boundary +14
        (16, "firm"),
        (29, "firm"),      # just below final
        (30, "final"),     # boundary +30
        (35, "final"),
        (365, "final"),    # never escalates past the top
    ],
)
def test_bucket_selection(days, expected):
    assert stage_name(policy().select_stage(invoice(days), set(), AS_OF)) == expected


# --- stop conditions --------------------------------------------------------

def test_paid_is_never_selected_even_when_overdue():
    assert policy().select_stage(invoice(35, status="paid"), set(), AS_OF) is None


def test_void_is_never_selected_even_when_overdue():
    assert policy().select_stage(invoice(35, status="void"), set(), AS_OF) is None


def test_do_not_contact_is_never_selected():
    assert policy().select_stage(invoice(35, do_not_contact=True), set(), AS_OF) is None


# --- idempotency at the policy level ---------------------------------------

def test_already_sent_current_stage_returns_none():
    assert policy().select_stage(invoice(3), {"friendly"}, AS_OF) is None


def test_already_sent_lower_stage_still_escalates_to_current_bucket():
    # Friendly already went out; invoice has aged into the firm bucket.
    assert stage_name(policy().select_stage(invoice(16), {"friendly"}, AS_OF)) == "firm"


def test_already_sent_top_stage_returns_none_forever():
    assert policy().select_stage(invoice(120), {"final"}, AS_OF) is None


def test_already_sent_all_lower_then_final_bucket_returns_final():
    assert stage_name(policy().select_stage(invoice(40), {"friendly", "firm"}, AS_OF)) == "final"


def test_same_bucket_repeat_run_returns_none():
    # firm already sent, invoice still in firm bucket -> nothing new
    assert policy().select_stage(invoice(20), {"firm"}, AS_OF) is None


# --- config-driven, not hardcoded ------------------------------------------

def test_thresholds_come_from_config_not_constants():
    custom = [
        Stage(name="nudge", min_days_overdue=7, tone="friendly", template="friendly.txt.j2"),
        Stage(name="escalate", min_days_overdue=45, tone="final", template="final.txt.j2"),
    ]
    p = DunningPolicy(custom)
    assert p.select_stage(invoice(3), set(), AS_OF) is None       # below new floor
    assert stage_name(p.select_stage(invoice(7), set(), AS_OF)) == "nudge"
    assert stage_name(p.select_stage(invoice(44), set(), AS_OF)) == "nudge"
    assert stage_name(p.select_stage(invoice(45), set(), AS_OF)) == "escalate"


def test_stages_need_not_be_pre_sorted():
    # Same ladder, shuffled — policy must sort internally.
    shuffled = [DEFAULT_STAGES[2], DEFAULT_STAGES[0], DEFAULT_STAGES[1]]
    assert stage_name(DunningPolicy(shuffled).select_stage(invoice(16), set(), AS_OF)) == "firm"


# --- first-contact cap (cold-start / backlog safety) ------------------------
# Production safety knob: an invoice that has NEVER been contacted should not
# open with a harsh stage just because it is very overdue (the backlog problem
# you hit the first time you point this at a real billing system). With a cap
# set, the FIRST send is held down to the cap; later runs escalate normally.


def capped(cap):
    return DunningPolicy(DEFAULT_STAGES, first_contact_stage_cap=cap)


def test_cap_holds_first_contact_down_from_final_to_friendly():
    # +35 would normally be `final`; with no prior contact, cap to friendly.
    assert stage_name(capped("friendly").select_stage(invoice(35), set(), AS_OF)) == "friendly"


def test_cap_holds_first_contact_down_from_firm_to_friendly():
    assert stage_name(capped("friendly").select_stage(invoice(16), set(), AS_OF)) == "friendly"


def test_cap_to_firm_holds_first_contact_at_firm():
    # A higher cap: first contact for a +35 invoice lands at firm, not final.
    assert stage_name(capped("firm").select_stage(invoice(35), set(), AS_OF)) == "firm"


def test_cap_never_raises_a_lower_bucket():
    # +3 only qualifies for friendly; a firm cap must NOT push it up to firm.
    assert stage_name(capped("firm").select_stage(invoice(3), set(), AS_OF)) == "friendly"


def test_cap_does_not_apply_once_a_reminder_has_been_sent():
    # Not first contact anymore: friendly already went out, invoice is +35 ->
    # it escalates to its true bucket (final), cap no longer applies.
    assert stage_name(capped("friendly").select_stage(invoice(35), {"friendly"}, AS_OF)) == "final"


def test_cap_does_not_resurrect_a_not_due_invoice():
    # The cap only ever lowers an otherwise-selected stage; it cannot create one.
    assert capped("friendly").select_stage(invoice(-20), set(), AS_OF) is None


def test_cap_still_respects_stop_conditions():
    assert capped("friendly").select_stage(invoice(35, status="paid"), set(), AS_OF) is None
    assert capped("friendly").select_stage(invoice(35, do_not_contact=True), set(), AS_OF) is None


def test_no_cap_is_the_default_and_keeps_highest_bucket():
    # Default (unset) cap must not change today's behavior: +35 first contact -> final.
    assert stage_name(policy().select_stage(invoice(35), set(), AS_OF)) == "final"


def test_unknown_cap_stage_name_is_rejected_at_construction():
    with pytest.raises(ValueError):
        DunningPolicy(DEFAULT_STAGES, first_contact_stage_cap="nonexistent")
