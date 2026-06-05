"""Cold-start guardrail — the #1 money-safety property, enforced in CODE.

The first unattended `run --send` against a REAL source (csv/quickbooks_*) with an
EMPTY audit trail and NO first_contact_stage_cap would open a long-overdue backlog
with FINAL notices. So `run --send` refuses that exact situation unless the operator
either sets the cap or explicitly passes --allow-cold-start. This enforces the
safety regardless of which config file got copied (the demo config keeps the cap
off on purpose), and never triggers for the mock demo.
"""
import io
from datetime import datetime, timezone
from pathlib import Path

import yaml

from reminders.cli import main
from reminders.config import load_config
from reminders.models import SendResult
from reminders.state import ReminderStateStore

REPO = Path(__file__).resolve().parent.parent
SAMPLE_CSV = REPO / "fixtures" / "magazine_manager_ar_export_sample.csv"
TEMPLATES = REPO / "templates"
SEATBELT = {"REMINDERS_ALLOW_SEND": "1"}


def csv_config(tmp_path, *, cap=None):
    dunning = {"stages": [
        {"name": "friendly", "min_days_overdue": 1, "tone": "friendly", "template": "friendly.txt.j2"},
        {"name": "firm", "min_days_overdue": 14, "tone": "firm", "template": "firm.txt.j2"},
        {"name": "final", "min_days_overdue": 30, "tone": "final", "template": "final.txt.j2"},
    ]}
    if cap:
        dunning["first_contact_stage_cap"] = cap
    obj = {
        "sender": {"name": "Pub Billing", "email": "b@x.example",
                   "reply_to": "b@x.example", "payment_contact": "b@x.example"},
        "templates_dir": str(TEMPLATES),
        "source": {"kind": "csv", "csv_path": str(SAMPLE_CSV)},
        "dunning": dunning,
        "state": {"db_path": str(tmp_path / "state.sqlite3")},
        "smtp": {"host": "smtp.invalid", "port": 587},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(obj))
    return str(p)


def run_send(cfg, *extra, env=SEATBELT):
    buf = io.StringIO()
    code = main(["--config", cfg, "--as-of", "2026-06-05", "run", "--send", *extra],
                out=buf, env=env)
    return code, buf.getvalue()


def test_cold_start_real_source_no_cap_is_refused(tmp_path):
    code, text = run_send(csv_config(tmp_path))           # csv, empty DB, no cap
    assert code == 2
    low = text.lower()
    assert "cold start" in low or "first_contact_stage_cap" in low
    assert "--allow-cold-start" in text                    # tells the operator the override


def test_cold_start_allowed_when_cap_is_set(tmp_path):
    code, _ = run_send(csv_config(tmp_path, cap="friendly"))
    assert code == 0


def test_cold_start_allowed_with_explicit_flag(tmp_path):
    code, _ = run_send(csv_config(tmp_path), "--allow-cold-start")
    assert code == 0


def test_cold_start_does_not_apply_to_mock_source(config_path):
    # The mock demo (conftest config_path) must never be blocked.
    code, _ = run_send(config_path)
    assert code == 0


def test_no_cold_start_once_the_audit_trail_is_non_empty(tmp_path):
    cfg = csv_config(tmp_path)                              # no cap
    store = ReminderStateStore(load_config(cfg).state.db_path)
    try:                                                   # seed one prior send
        store.record_send(
            SendResult(invoice_id="SEED", stage="friendly", channel="email",
                       success=True, detail="seed", message_hash="abc",
                       sent_at=datetime(2026, 6, 1, tzinfo=timezone.utc)),
            to_email="seed@x.example", batch_id="seed")
    finally:
        store.close()
    code, _ = run_send(cfg)                                 # not a cold start anymore
    assert code == 0


def test_cold_start_refusal_is_json_when_requested(tmp_path):
    import json
    buf = io.StringIO()
    code = main(["--config", csv_config(tmp_path), "--as-of", "2026-06-05",
                 "run", "--send", "--json"], out=buf, env=SEATBELT)
    assert code == 2
    assert json.loads(buf.getvalue())["error"] == "cold_start_unsafe"
