"""Configuration models + loader.

All thresholds, tone tiers, and the sender identity live in config.yaml — never
hardcoded. Secrets come from the environment (.env is auto-loaded), referenced
in YAML as ``${VAR}`` and expanded at load time.
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel

try:  # optional convenience — load .env if python-dotenv is installed
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False


class Stage(BaseModel):
    """One rung of the dunning ladder. Pure configuration — no behavior."""

    model_config = {"frozen": True}

    name: str
    min_days_overdue: int
    tone: str
    template: str


class SenderConfig(BaseModel):
    name: str
    email: str
    reply_to: str
    payment_contact: str


class QBOConfig(BaseModel):
    """QuickBooks Online (REST/OAuth2). Secrets come from .env via ${VAR}."""

    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    realm_id: str = ""                    # the QuickBooks company id
    environment: str = "production"       # "production" | "sandbox"
    minor_version: int = 65


class QBDConfig(BaseModel):
    """QuickBooks Desktop (qbXML via the Web Connector). The Web Connector pushes
    to a SOAP endpoint we host; that endpoint writes a parsed-invoice cache which
    this adapter reads. See sources/quickbooks_desktop.py."""

    qbxml_version: str = "13.0"
    cache_path: str = "qbd_invoices_cache.json"
    home_currency: str = "USD"


class SourceConfig(BaseModel):
    kind: str = "mock"
    fixture_path: str | None = None       # for kind=mock
    csv_path: str | None = None           # for kind=csv (Magazine Manager export)
    quickbooks_online: QBOConfig | None = None
    quickbooks_desktop: QBDConfig | None = None


class DunningConfig(BaseModel):
    stages: list[Stage]
    # Cold-start / backlog safety. If set to a stage name, the FIRST reminder
    # ever sent for an invoice is held down to this stage (it escalates normally
    # afterward). None = no cap (highest-bucket-wins). Recommended: "friendly"
    # for real deployments; left unset for the mock demo. See README.
    first_contact_stage_cap: str | None = None


class StateConfig(BaseModel):
    db_path: str = "reminders_state.sqlite3"


class SmtpConfig(BaseModel):
    host: str = "localhost"
    port: int = 587
    use_tls: bool = True
    username: str = ""
    password: str = ""


class ToneRewriteConfig(BaseModel):
    """Optional LLM tone-rewrite. OFF by default — the system is fully
    deterministic unless this is explicitly enabled. The LLM only rephrases body
    copy at send-time; it never sees or decides amounts, stages, or recipients.
    """

    enabled: bool = False
    model: str = "claude-opus-4-8"
    max_tokens: int = 1024


class AutomationConfig(BaseModel):
    """Unattended (cron) send mode. Ships OFF. When on, sends the *routine* lane
    automatically and diverts the irreversible slice (final notices, high-value,
    first-ever contacts) to the human approval queue. The dangerous-slice gate is
    additionally enforced in code (see reminders.automation) — config can only make
    it MORE conservative, never less. See README "Running unattended"."""

    enabled: bool = False                         # master kill switch (authorization)
    hold_flag_path: str | None = None             # if this file exists, refuse all sends
    csv_max_age_hours: float = 26.0               # refuse if the export is older than this
    auto_stages: list[str] = ["friendly", "firm"]  # 'final' is NEVER auto (also code-enforced)
    max_auto_amount: Decimal = Decimal("1000")    # config ceiling (capped again in code)
    max_send_per_run: int = 25                    # a malformed export can't blast everyone
    min_open_invoices: int = 1                    # empty/header-only export is a LOUD failure
    min_amount: Decimal = Decimal("1")            # quarantine $0 / negative (paid / credits)
    max_amount: Decimal = Decimal("150000")       # quarantine implausible amounts
    require_status_column: bool = True            # missing status column refuses (no fail-open)
    require_dnc_column: bool = True               # missing do_not_contact column refuses
    summary_to: str = ""                          # email address for the post-run summary/alerts


class Config(BaseModel):
    sender: SenderConfig
    source: SourceConfig
    dunning: DunningConfig
    state: StateConfig
    smtp: SmtpConfig
    tone_rewrite: ToneRewriteConfig = ToneRewriteConfig()
    automation: AutomationConfig = AutomationConfig()
    templates_dir: str = "templates"


def _expand_env(value):
    """Recursively expand ``${VAR}`` placeholders in strings using os.environ.

    Unknown vars are left intact (loading config must never *require* secrets —
    only sending does), so a missing SMTP password can't break a dry-run.
    """
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: str | Path) -> Config:
    """Load and validate config.yaml, expanding ${ENV} secrets (after .env)."""
    load_dotenv()  # populate os.environ from .env if present; never overrides set vars
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return Config.model_validate(_expand_env(raw))
