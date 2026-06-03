"""Tests for the CLI-level logging helpers: remote resolution and consent."""

import pytest

from aicodinggym import cli


# ── _resolve_logs_remote: one repo for all, SWE-only clone fallback ──────────

def test_swe_falls_back_to_problem_repo_when_no_submission_repo():
    assert cli._resolve_logs_remote("swe", {"repo_url": "git@h:u/r.git"}, {}, None) \
        == "git@h:u/r.git"


def test_submission_repo_takes_precedence_for_all_benchmarks():
    cfg = {"submission_repo_url": "git@h:u/own.git"}
    assert cli._resolve_logs_remote("swe", {"repo_url": "git@h:u/prob.git"}, cfg, None) \
        == "git@h:u/own.git"
    assert cli._resolve_logs_remote("cr", None, cfg, None) == "git@h:u/own.git"
    assert cli._resolve_logs_remote("mle", None, cfg, None) == "git@h:u/own.git"


def test_cr_never_uses_the_readonly_clone():
    # CR creds carry the read-only PR repo_url; it must NOT become the target.
    assert cli._resolve_logs_remote("cr", {"repo_url": "git@h:upstream/pr.git"}, {}, None) \
        is None


def test_mle_without_submission_repo_is_none():
    assert cli._resolve_logs_remote("mle", None, {}, None) is None


def test_override_wins():
    cfg = {"submission_repo_url": "git@h:u/own.git"}
    assert cli._resolve_logs_remote("swe", {"repo_url": "x"}, cfg, "git@h:u/override.git") \
        == "git@h:u/override.git"


def test_env_var_used_before_config(monkeypatch):
    monkeypatch.setenv("AICODINGGYM_LOGS_REMOTE", "git@h:u/env.git")
    assert cli._resolve_logs_remote("swe", {"repo_url": "x"}, {}, None) == "git@h:u/env.git"


# ── _resolve_log_upload_consent: flag > stored > prompt; non-tty is safe ──────

@pytest.fixture(autouse=True)
def _isolate_consent(monkeypatch):
    """Keep consent in memory so the prompt logic is tested without touching disk."""
    store = {"value": None}
    monkeypatch.setattr(cli, "get_logging_consent", lambda: store["value"])
    monkeypatch.setattr(cli, "set_logging_consent", lambda v: store.__setitem__("value", v))
    return store


def test_explicit_flag_is_persisted_and_returned(_isolate_consent):
    assert cli._resolve_log_upload_consent(True) is True
    assert _isolate_consent["value"] is True
    assert cli._resolve_log_upload_consent(False) is False
    assert _isolate_consent["value"] is False


def test_stored_consent_is_used_without_prompting(_isolate_consent, monkeypatch):
    _isolate_consent["value"] = True
    # If this tried to prompt, confirm() would raise in a non-tty; ensure it doesn't.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli._resolve_log_upload_consent(None) is True


def test_non_tty_without_record_defaults_to_no_upload(_isolate_consent, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli._resolve_log_upload_consent(None) is False
    assert _isolate_consent["value"] is None  # nothing recorded


def test_interactive_prompt_is_recorded(_isolate_consent, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(cli.click, "confirm", lambda *a, **k: True)
    assert cli._resolve_log_upload_consent(None) is True
    assert _isolate_consent["value"] is True


# ── _configure_hint ──────────────────────────────────────────────────────────

def test_configure_hint_includes_user_id():
    assert cli._configure_hint("alice") == "aicodinggym configure --user-id alice"
    assert cli._configure_hint(None) == "aicodinggym configure"
