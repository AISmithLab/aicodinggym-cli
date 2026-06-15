"""Tests for logging-consent storage and the submission-repo allowlist field."""

import json

import pytest

from aicodinggym import config


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config at a throwaway dir so we never touch the real ~/.aicodinggym."""
    cfg_dir = tmp_path / ".aicodinggym"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_dir / "config.json")
    monkeypatch.setattr(config, "CREDENTIALS_PATH", cfg_dir / "credentials.json")
    return cfg_dir


def test_consent_roundtrip(isolated_config):
    assert config.get_logging_consent() is None  # never asked
    config.set_logging_consent(True)
    assert config.get_logging_consent() is True
    config.set_logging_consent(False)
    assert config.get_logging_consent() is False


def test_consent_persists_as_string_in_allowlist(isolated_config):
    config.set_logging_consent(True)
    raw = json.loads(config.CONFIG_PATH.read_text())
    assert raw["entire_logging_consent"] == "granted"


def test_submission_repo_url_survives_save(isolated_config):
    cfg = config.load_config()
    cfg["user_id"] = "alice"
    cfg["submission_repo_url"] = "git@aicodinggym.com:alice/sub.git"
    config.save_config(cfg)

    reloaded = config.load_config()
    assert reloaded["submission_repo_url"] == "git@aicodinggym.com:alice/sub.git"


def test_consent_and_submission_repo_coexist(isolated_config):
    cfg = config.load_config()
    cfg["submission_repo_url"] = "git@h:u/r.git"
    config.save_config(cfg)
    config.set_logging_consent(True)  # separate load+save must not drop the URL
    assert config.load_config().get("submission_repo_url") == "git@h:u/r.git"
    assert config.get_logging_consent() is True


def test_unknown_fields_are_filtered(isolated_config):
    config.save_config({"user_id": "a", "bogus": "x"})
    assert "bogus" not in config.load_config()
