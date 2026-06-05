"""Config loader: parse YAML, build typed config, expand ${ENV} secrets."""
import textwrap

from reminders.config import Config, load_config

YAML = textwrap.dedent(
    """
    sender:
      name: "Test Billing"
      email: "billing@test.example"
      reply_to: "billing@test.example"
      payment_contact: "billing@test.example or call 555-0000"
    source:
      kind: "mock"
      fixture_path: "fixtures/sample_invoices.json"
    dunning:
      stages:
        - {name: friendly, min_days_overdue: 1, tone: friendly, template: friendly.txt.j2}
        - {name: firm, min_days_overdue: 14, tone: firm, template: firm.txt.j2}
        - {name: final, min_days_overdue: 30, tone: final, template: final.txt.j2}
    state:
      db_path: "reminders_state.sqlite3"
    smtp:
      host: "smtp.gmail.com"
      port: 587
      use_tls: true
      username: "${SMTP_USERNAME}"
      password: "${SMTP_PASSWORD}"
    """
)


def write_cfg(tmp_path, text=YAML):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_loads_typed_config(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert isinstance(cfg, Config)
    assert cfg.sender.name == "Test Billing"
    assert cfg.source.kind == "mock"
    assert cfg.state.db_path == "reminders_state.sqlite3"


def test_stages_parsed_in_order(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    names = [s.name for s in cfg.dunning.stages]
    assert names == ["friendly", "firm", "final"]
    assert cfg.dunning.stages[1].min_days_overdue == 14


def test_env_vars_are_expanded_for_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_USERNAME", "smtp-user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-specific-secret")
    cfg = load_config(write_cfg(tmp_path))
    assert cfg.smtp.username == "smtp-user@example.com"
    assert cfg.smtp.password == "app-specific-secret"


def test_unset_env_var_does_not_crash_loading(tmp_path, monkeypatch):
    # Loading config must never require secrets — only sending does.
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    cfg = load_config(write_cfg(tmp_path))
    # Unexpanded placeholder is left intact rather than raising at load time.
    assert "SMTP_USERNAME" in cfg.smtp.username
