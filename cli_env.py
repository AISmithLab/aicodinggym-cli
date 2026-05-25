"""Detect AI coding tool + model used for the current shell session.

Reads only an allowlist of well-known env vars — never the full environment —
so secrets cannot accidentally leak into the submission payload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ALLOWED_TOOLS = (
    "claude-code",
    "cursor",
    "antigravity",
    "aider",
    "codex-cli",
    "copilot-cli",
    "windsurf",
    "continue",
    "cline",
    "gemini-cli",
)


def detect_tool() -> tuple[str | None, str | None]:
    """Return (tool_name, version) inferred from env signals. None if unknown."""
    if os.environ.get("CLAUDECODE") == "1":
        return ("claude-code", _version("claude"))
    if os.environ.get("CURSOR_TRACE_ID") or os.environ.get("TERM_PROGRAM") == "cursor":
        return ("cursor", os.environ.get("CURSOR_VERSION"))
    if os.environ.get("ANTIGRAVITY"):
        return ("antigravity", os.environ.get("ANTIGRAVITY_VERSION"))
    if os.environ.get("AIDER_MODEL") or shutil.which("aider"):
        return ("aider", _version("aider"))
    if os.environ.get("CODEX_CLI"):
        return ("codex-cli", _version("codex"))
    if os.environ.get("WINDSURF"):
        return ("windsurf", os.environ.get("WINDSURF_VERSION"))
    if os.environ.get("CONTINUE_CLI"):
        return ("continue", _version("continue"))
    if os.environ.get("CLINE_CLI"):
        return ("cline", _version("cline"))
    if os.environ.get("GEMINI_CLI"):
        return ("gemini-cli", _version("gemini"))
    return (None, None)


def detect_model() -> str | None:
    """Best-effort model detection from env. Lowercase, trimmed."""
    raw = (
        os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("CLAUDE_CODE_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or os.environ.get("AIDER_MODEL")
        or os.environ.get("GEMINI_MODEL")
        or os.environ.get("CURSOR_MODEL")
    )
    if not raw:
        return None
    return raw.strip().lower()


def resolve(
    cli_tool: str | None,
    cli_version: str | None,
    cli_model: str | None,
) -> dict[str, str | None]:
    """CLI flags win; env detection fills the gaps. Returns kwargs for api.py."""
    auto_tool, auto_ver = detect_tool()
    return {
        "tool": cli_tool or auto_tool,
        "tool_version": cli_version or auto_ver,
        "ai_model": cli_model or detect_model(),
    }


def read_solution_log_model(problem_dir: Path) -> str | None:
    """For MLE: prefer the model recorded in solution_log.json (set by the agent
    after each prompt per CLAUDE.md). Falls back to None if missing or malformed.
    """
    log_path = problem_dir / "solution_log.json"
    if not log_path.exists():
        return None
    try:
        data = json.loads(log_path.read_text())
    except Exception:
        return None

    # Tolerate a few common shapes: {"model": "..."} or {"model_id": "..."}
    # or {"entries": [{"model": "..."}, ...]} — take the most recent one.
    if isinstance(data, dict):
        if isinstance(data.get("model"), str):
            return data["model"].strip().lower()
        if isinstance(data.get("model_id"), str):
            return data["model_id"].strip().lower()
        entries = data.get("entries")
        if isinstance(entries, list) and entries:
            last = entries[-1]
            if isinstance(last, dict):
                for key in ("model", "model_id"):
                    if isinstance(last.get(key), str):
                        return last[key].strip().lower()
    return None


def _version(cmd: str) -> str | None:
    if not shutil.which(cmd):
        return None
    try:
        out = subprocess.check_output(
            [cmd, "--version"],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
        # Keep the last token (often "1.2.3") and cap length.
        token = out.strip().splitlines()[0].split()[-1]
        return token[:32]
    except Exception:
        return None
