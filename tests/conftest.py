"""Shared test fixtures: a temp config.yaml wired to an isolated SQLite DB but
the real repo fixtures and templates."""
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "fixtures" / "sample_invoices.json"
TEMPLATES = REPO_ROOT / "templates"


@pytest.fixture
def config_path(tmp_path):
    """Write a config.yaml in tmp_path with an isolated DB; return its path (str)."""
    db = tmp_path / "state.sqlite3"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(f"""
        sender:
          name: "Acme Magazine — Billing"
          email: "billing@acme.example"
          reply_to: "billing@acme.example"
          payment_contact: "billing@acme.example or call (555) 123-4567"
        templates_dir: "{TEMPLATES}"
        source:
          kind: "mock"
          fixture_path: "{FIXTURE}"
        dunning:
          stages:
            - {{name: friendly, min_days_overdue: 1, tone: friendly, template: friendly.txt.j2}}
            - {{name: firm, min_days_overdue: 14, tone: firm, template: firm.txt.j2}}
            - {{name: final, min_days_overdue: 30, tone: final, template: final.txt.j2}}
        state:
          db_path: "{db}"
        smtp:
          host: "smtp.invalid"
          port: 587
          use_tls: true
          username: "noreply@acme.example"
          password: "unused-in-tests"
    """))
    return str(cfg)
