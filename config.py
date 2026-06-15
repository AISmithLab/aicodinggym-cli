"""Configuration and credentials management for AI Coding Gym CLI.

Stores configuration in ~/.aicodinggym/config.json and per-problem
credentials in ~/.aicodinggym/credentials.json.
SSH keys are stored in ~/.aicodinggym/{user_id}_id_rsa.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


CONFIG_DIR = Path.home() / ".aicodinggym"
CONFIG_PATH = CONFIG_DIR / "config.json"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"

# Fields persisted in config.json
_CONFIG_FIELDS = (
    "user_id", "repo_name", "private_key_path", "workspace_dir",
    # Writable repo to push AI-session logs to (Entire integration). Used for
    # CR/MLE where the cloned repo is read-only or absent.
    "submission_repo_url",
    # AI-session upload consent: "granted" | "declined" (absent = not yet asked).
    "entire_logging_consent",
)


def ensure_config_dir() -> Path:
    """Create the config directory with secure permissions if it doesn't exist.

    On Unix/macOS: mode 0o700 (owner-only access).
    On Windows: removes inherited ACLs and grants full control only to the
    current user via icacls.
    """
    created = not CONFIG_DIR.exists()
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created and sys.platform == "win32":
        username = os.environ.get("USERNAME", "")
        if username:
            subprocess.run(
                ["icacls", str(CONFIG_DIR), "/inheritance:r",
                 "/grant:r", f"{username}:(OI)(CI)(F)"],
                capture_output=True,
            )
    return CONFIG_DIR


def load_config() -> dict[str, str]:
    """Load global configuration from ~/.aicodinggym/config.json."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in _CONFIG_FIELDS and isinstance(v, str) and v}
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(config: dict[str, str]) -> None:
    """Persist global configuration to ~/.aicodinggym/config.json."""
    ensure_config_dir()
    data = {k: v for k, v in config.items() if k in _CONFIG_FIELDS}
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")


def load_credentials() -> dict[str, dict[str, Any]]:
    """Load per-problem credentials from ~/.aicodinggym/credentials.json."""
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def save_credentials(credentials: dict[str, dict[str, Any]]) -> None:
    """Persist per-problem credentials to ~/.aicodinggym/credentials.json."""
    ensure_config_dir()
    CREDENTIALS_PATH.write_text(json.dumps(credentials, indent=2) + "\n")


def get_logging_consent() -> bool | None:
    """Return AI-session upload consent: True/False, or None if never asked."""
    value = load_config().get("entire_logging_consent")
    if value == "granted":
        return True
    if value == "declined":
        return False
    return None


def set_logging_consent(granted: bool) -> None:
    """Persist the user's AI-session upload consent choice."""
    config = load_config()
    config["entire_logging_consent"] = "granted" if granted else "declined"
    save_config(config)


def require_config(config: dict[str, str], field: str, label: str) -> str:
    """Get a required config field or raise a descriptive error."""
    value = config.get(field)
    if not value:
        raise ConfigError(
            f"{label} is not configured.\n\n"
            f"Run 'aicodinggym configure --user-id YOUR_USER_ID' first to set up your credentials.\n"
            f"This generates an SSH key and registers it with the AI Coding Gym server."
        )
    return value


class ConfigError(Exception):
    """Raised when required configuration is missing."""
    pass
