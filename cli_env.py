"""Detect AI coding tool + model used for the current shell session.

Reads only an allowlist of well-known env vars — never the full environment —
so secrets cannot accidentally leak into the submission payload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import load_attribution

# Universal self-report file. Any AI coding tool can write this into the
# challenge folder (instructed via AGENTS.md) to declare the tool + model it
# runs as. This is the catch-all that makes attribution work for *every* tool
# — the agent self-identifies instead of us reverse-engineering each tool's
# private on-disk format.
AGENT_REPORT_FILENAME = ".gym_attribution.json"

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

# Substring → canonical tool name. Lowercased process-image basenames are
# matched against the keys; first match wins so order matters (longer / more
# specific substrings first).
_PROCESS_NAME_MAP: tuple[tuple[str, str], ...] = (
    ("antigravity", "antigravity"),
    ("claude", "claude-code"),
    ("cursor", "cursor"),
    ("windsurf", "windsurf"),
    ("gemini", "gemini-cli"),
    ("codex", "codex-cli"),
    ("aider", "aider"),
    ("cline", "cline"),
    ("continue", "continue"),
    ("copilot", "copilot-cli"),
)

# CLI binary to invoke for ``--version`` per tool. Missing entries mean we
# don't know how to interrogate that tool for a version string.
_TOOL_VERSION_CMD: dict[str, str] = {
    "claude-code": "claude",
    "aider": "aider",
    "codex-cli": "codex",
    "gemini-cli": "gemini",
    "cursor": "cursor",
    "windsurf": "windsurf",
}


def detect_tool() -> tuple[str | None, str | None]:
    """Return (tool_name, version) inferred from env signals or the process
    tree. Falls back to (None, None) if no tool is identifiable.
    """
    if os.environ.get("CLAUDECODE") == "1":
        return ("claude-code", _version("claude"))
    if os.environ.get("CURSOR_TRACE_ID") or os.environ.get("TERM_PROGRAM") == "cursor":
        return ("cursor", os.environ.get("CURSOR_VERSION") or _version("cursor"))
    if os.environ.get("ANTIGRAVITY"):
        return ("antigravity", os.environ.get("ANTIGRAVITY_VERSION"))
    if os.environ.get("AIDER_MODEL"):
        return ("aider", _version("aider"))
    if os.environ.get("CODEX_CLI"):
        return ("codex-cli", _version("codex"))
    if os.environ.get("WINDSURF"):
        return ("windsurf", os.environ.get("WINDSURF_VERSION") or _version("windsurf"))
    if os.environ.get("CONTINUE_CLI"):
        return ("continue", _version("continue"))
    if os.environ.get("CLINE_CLI"):
        return ("cline", _version("cline"))
    if os.environ.get("GEMINI_CLI"):
        return ("gemini-cli", _version("gemini"))

    # Process-tree fallback: walk parent processes and match well-known
    # tool binary names. Reliable even when the tool itself doesn't export
    # any environment variable.
    tool = detect_tool_from_process_tree()
    if tool:
        cmd = _TOOL_VERSION_CMD.get(tool)
        return (tool, _version(cmd) if cmd else None)
    return (None, None)


def detect_tool_from_process_tree() -> str | None:
    """Walk ancestor processes; return the first matching tool name.

    Uses ``psutil`` when available (cross-platform, robust). Falls back to
    platform-specific stdlib probes (``ps`` on POSIX, PowerShell/CIM on
    Windows). Returns None when no known tool name is seen in the chain.
    """
    for name in _process_ancestor_names():
        lowered = name.lower()
        if lowered.endswith(".exe"):
            lowered = lowered[:-4]
        for needle, tool in _PROCESS_NAME_MAP:
            if needle in lowered:
                return tool
    return None


def _process_ancestor_names(max_depth: int = 16) -> list[str]:
    """Return ancestor process image names (current → root), capped at
    ``max_depth`` entries to avoid pathological loops.
    """
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        try:
            names: list[str] = []
            proc = psutil.Process()
            while proc and len(names) < max_depth:
                try:
                    names.append(proc.name() or "")
                except Exception:
                    break
                try:
                    proc = proc.parent()
                except Exception:
                    break
            return [n for n in names if n]
        except Exception:
            pass

    if sys.platform == "win32":
        return _ancestor_names_windows(max_depth)
    return _ancestor_names_posix(max_depth)


def _ancestor_names_posix(max_depth: int) -> list[str]:
    names: list[str] = []
    pid = os.getppid()
    seen: set[int] = set()
    while pid and pid > 1 and len(names) < max_depth and pid not in seen:
        seen.add(pid)
        try:
            out = subprocess.check_output(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                text=True, timeout=2, stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            break
        parts = out.split(None, 1)
        if len(parts) < 2:
            break
        try:
            ppid = int(parts[0])
        except ValueError:
            break
        names.append(parts[1].strip())
        pid = ppid
    return names


def _ancestor_names_windows(max_depth: int) -> list[str]:
    """Build the full PID→(Name,PPID) map once via PowerShell, then walk.

    Spawning PowerShell N times for a chain is slow; one snapshot is enough.
    """
    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId,Name | "
                "ConvertTo-Json -Compress",
            ],
            text=True, timeout=5, stderr=subprocess.DEVNULL,
        )
        data = json.loads(out) if out.strip() else []
    except Exception:
        return []

    if isinstance(data, dict):
        data = [data]
    table: dict[int, tuple[int, str]] = {}
    for row in data:
        try:
            pid = int(row.get("ProcessId"))
            ppid = int(row.get("ParentProcessId"))
            name = str(row.get("Name") or "")
        except (TypeError, ValueError):
            continue
        table[pid] = (ppid, name)

    names: list[str] = []
    pid = os.getppid()
    seen: set[int] = set()
    while pid and pid not in seen and len(names) < max_depth:
        seen.add(pid)
        entry = table.get(pid)
        if not entry:
            break
        ppid, name = entry
        if name:
            names.append(name)
        pid = ppid
    return names


def detect_model() -> str | None:
    """Best-effort model detection.

    Order: explicit env vars, then a tool-aware reader for whichever coding
    tool we detected. The tool-aware path is what makes the auto path
    actually work for the major CLIs (Claude Code, Codex CLI, Aider) since
    none of them export their model to the shell environment.
    """
    raw = (
        os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("CLAUDE_CODE_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or os.environ.get("AIDER_MODEL")
        or os.environ.get("GEMINI_MODEL")
        or os.environ.get("CURSOR_MODEL")
    )
    if raw:
        return raw.strip().lower()

    tool, _ = detect_tool()
    if tool == "claude-code":
        return read_claude_code_session_model()
    if tool == "codex-cli":
        return read_codex_session_model() or read_codex_config_model()
    if tool == "aider":
        # AIDER_MODEL already covered by the env block above; nothing else
        # is reliably written to disk by aider.
        return None
    return None


def read_codex_config_model() -> str | None:
    """Return the default model from ``~/.codex/config.toml``.

    Codex CLI persists ``model = "<name>"`` as the top-level default. We
    avoid a full TOML parse (``tomllib`` is 3.11+) and just scan for the
    first top-level ``model`` assignment before any ``[section]`` header.
    """
    cfg = Path.home() / ".codex" / "config.toml"
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            break  # entered a sub-section; the top-level default lives above
        if stripped.startswith("model"):
            # Match: model = "name"  or  model="name"
            _, _, rhs = stripped.partition("=")
            value = rhs.strip().strip('"').strip("'")
            if value:
                return value.lower()
    return None


def read_codex_session_model(cwd: Path | None = None) -> str | None:
    """Return the newest model from a Codex CLI session log whose ``cwd``
    matches the current working directory (or any ancestor).

    Sessions live under ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
    and contain a ``session_meta`` line with the originating ``cwd`` and
    ``"model":"..."`` fields throughout. The newest matching file wins.
    """
    cwd = (cwd or Path.cwd()).resolve()
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.is_dir():
        return None

    candidate_cwds = {str(p).lower() for p in (cwd, *cwd.parents)}

    try:
        files = sorted(
            sessions_root.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for jsonl in files[:8]:
        meta_cwd = _codex_session_cwd(jsonl)
        if meta_cwd is None or meta_cwd.lower() not in candidate_cwds:
            continue
        model = _scan_codex_jsonl_for_model(jsonl)
        if model:
            return model
    return None


def _scan_codex_jsonl_for_model(path: Path, max_bytes: int = 256 * 1024) -> str | None:
    """Tail a Codex session JSONL and return the newest ``payload.model``."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            tail = f.read()
    except OSError:
        return None
    for raw in reversed(tail.splitlines()):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if isinstance(payload, dict):
            model = payload.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip().lower()
    return None


def _codex_session_cwd(path: Path) -> str | None:
    """Read just the first line of a Codex session log and return its cwd."""
    try:
        with open(path, "rb") as f:
            first = f.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        obj = json.loads(first)
    except Exception:
        return None
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if isinstance(payload, dict):
        cwd_val = payload.get("cwd")
        if isinstance(cwd_val, str) and cwd_val:
            return cwd_val
    return None


def read_claude_code_session_model(cwd: Path | None = None) -> str | None:
    """Return newest assistant ``message.model`` from the Claude Code session
    transcript matching ``cwd`` (or any ancestor). None if nothing found.

    Claude Code writes per-session JSONL transcripts to
    ``~/.claude/projects/<slug>/<session-uuid>.jsonl`` where ``<slug>`` is the
    absolute working directory with ``:``, ``\\`` and ``/`` each replaced by
    ``-``. Each assistant line carries ``message.model`` (e.g.
    ``claude-opus-4-7``).
    """
    cwd = (cwd or Path.cwd()).resolve()
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None

    try:
        listing = {p.name.lower(): p for p in projects.iterdir() if p.is_dir()}
    except OSError:
        return None

    for ancestor in (cwd, *cwd.parents):
        slug = _claude_project_slug(ancestor)
        slug_dir = projects / slug
        if not slug_dir.is_dir():
            slug_dir = listing.get(slug.lower())
            if slug_dir is None:
                continue
        try:
            files = sorted(
                (p for p in slug_dir.glob("*.jsonl") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for jsonl in files[:3]:
            model = _scan_jsonl_for_model(jsonl)
            if model:
                return model
    return None


def _claude_project_slug(path: Path) -> str:
    s = str(path)
    for sep in (":", "\\", "/"):
        s = s.replace(sep, "-")
    return s


def _scan_jsonl_for_model(path: Path, max_bytes: int = 256 * 1024) -> str | None:
    """Tail the JSONL and return the newest non-synthetic assistant model."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard partial line
            tail = f.read()
    except OSError:
        return None
    for raw in reversed(tail.splitlines()):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if not isinstance(model, str):
            continue
        model = model.strip().lower()
        if model and model != "<synthetic>":
            return model
    return None


def read_agent_report(problem_dir: Path | None = None) -> dict[str, str | None]:
    """Read the agent self-reported attribution file (``.gym_attribution.json``).

    Any AI coding tool can write this file into the challenge folder (per
    AGENTS.md) to declare the tool + model it is running as. This is the
    universal capture path — it works for *every* tool/model because the agent
    self-identifies rather than us reverse-engineering each tool's on-disk
    format.

    Looks in ``problem_dir`` first, then the current working directory.
    Accepts both snake_case and camelCase keys, plus ``model`` as an alias for
    ``ai_model``. Missing or malformed files yield all-None.
    """
    empty: dict[str, str | None] = {"tool": None, "tool_version": None, "ai_model": None}

    candidates: list[Path] = []
    if problem_dir is not None:
        candidates.append(Path(problem_dir) / AGENT_REPORT_FILENAME)
    cwd_path = Path.cwd() / AGENT_REPORT_FILENAME
    if cwd_path not in candidates:
        candidates.append(cwd_path)

    def _clean(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        return {
            "tool": _clean(data.get("tool")),
            "tool_version": _clean(data.get("tool_version") or data.get("toolVersion")),
            "ai_model": _clean(
                data.get("ai_model") or data.get("aiModel") or data.get("model")
            ),
        }
    return empty


def resolve(
    cli_tool: str | None,
    cli_version: str | None,
    cli_model: str | None,
    problem_dir: Path | None = None,
) -> dict[str, str | None]:
    """Resolve attribution for a submission. Precedence (highest first):

    1. CLI flags (``--tool``, ``--tool-version``, ``--ai-model``)
    2. Live auto-detection (env vars, Claude Code session log, process tree)
    3. Agent self-report file (``.gym_attribution.json`` in the challenge dir)
    4. Persistent attribution config (``~/.aicodinggym/attribution.json``)

    Layer 2 is authoritative where it fires (real model string from the tool's
    own session transcript) but only covers tools we know how to read. Layer 3
    is the universal backstop: any agent can self-report, so attribution is
    captured for *every* tool/model with zero human input. Layer 4 is the
    human-set fallback (``aicodinggym set-attribution``).
    """
    auto_tool, auto_ver = detect_tool()
    auto_model = detect_model()
    agent = read_agent_report(problem_dir)
    persisted = load_attribution()

    # ``tool`` and ``tool_version`` are paired — the version always belongs to
    # whichever layer supplied the tool. Walk layers in precedence order and
    # take both fields from the first one that names a tool. ``--tool-version``
    # (cli_version) still overrides at the end.
    layers: tuple[tuple[str | None, str | None], ...] = (
        (cli_tool, cli_version),
        (auto_tool, auto_ver),
        (agent.get("tool"), agent.get("tool_version")),
        (persisted.get("tool"), persisted.get("tool_version")),
    )
    final_tool: str | None = None
    final_version: str | None = None
    for tool_layer, version_layer in layers:
        if tool_layer:
            final_tool = tool_layer
            final_version = version_layer
            break
    if cli_version:
        final_version = cli_version

    return {
        "tool": final_tool,
        "tool_version": final_version,
        "ai_model": cli_model or auto_model or agent.get("ai_model") or persisted.get("ai_model"),
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
        # First whitespace token of the first line — e.g. "claude --version"
        # prints "2.1.141 (Claude Code)" and we want "2.1.141", not "Code)".
        first_line = out.strip().splitlines()[0]
        for token in first_line.split():
            if any(ch.isdigit() for ch in token):
                return token[:32]
        return first_line.split()[0][:32]
    except Exception:
        return None
