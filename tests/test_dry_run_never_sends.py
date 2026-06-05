"""Dry-run is the safe default: identical output across runs, zero sends recorded."""
import io

from reminders.cli import main


def run_dry(config_path) -> str:
    buf = io.StringIO()
    code = main(
        ["--config", config_path, "--as-of", "2026-06-05", "run", "--dry-run"],
        out=buf,
        env={},  # no REMINDERS_ALLOW_SEND
    )
    assert code == 0
    return buf.getvalue()


def test_dry_run_is_the_default_mode(config_path):
    # `run` with no flag must behave exactly like `run --dry-run`.
    buf_default, buf_explicit = io.StringIO(), io.StringIO()
    main(["--config", config_path, "--as-of", "2026-06-05", "run"], out=buf_default, env={})
    main(["--config", config_path, "--as-of", "2026-06-05", "run", "--dry-run"], out=buf_explicit, env={})
    assert buf_default.getvalue() == buf_explicit.getvalue()


def test_two_dry_runs_produce_identical_output(config_path):
    first = run_dry(config_path)
    second = run_dry(config_path)
    assert first == second
    assert "DRY-RUN" in first
    assert "INV-1010" in first  # the whale was previewed


def test_dry_run_records_zero_sends(config_path):
    run_dry(config_path)
    run_dry(config_path)
    # The audit trail must still be empty after repeated dry-runs.
    buf = io.StringIO()
    main(["--config", config_path, "run"], out=io.StringIO(), env={})  # once more
    main(["--config", config_path, "history"], out=buf, env={})
    assert "0 record(s)" in buf.getvalue()
    assert "no sends recorded" in buf.getvalue()


def test_dry_run_cannot_send_even_with_seatbelt_set(config_path):
    # Dry-run must never send, regardless of the env seatbelt.
    buf = io.StringIO()
    main(["--config", config_path, "--as-of", "2026-06-05", "run", "--dry-run"],
         out=buf, env={"REMINDERS_ALLOW_SEND": "1"})
    hist = io.StringIO()
    main(["--config", config_path, "history"], out=hist, env={})
    assert "0 record(s)" in hist.getvalue()
