"""Persistent AI Coding Gym session log (chat + submissions).

Stores ``gym_log.json`` with versioned ``entries``. ``per_model_accuracy`` is
always local CV; ``ground_truth_accuracy`` is the gym leaderboard score
(submissions only). ``delta_from_last_submission`` compares consecutive
ground-truth scores only.

Summary table **CV Acc** column: uses ``ensemble_accuracy`` when set; else the
sole value in ``per_model_accuracy`` if there is exactly one key; otherwise
``best:<max>`` (two decimal places) over ``per_model_accuracy`` values.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

EntryType = Literal["chat", "submission"]

_LOG_VERSION = 2
_DEFAULT_FILENAME = "gym_log.json"
_explicit_path: Path | None = None


def set_log_path(path: str | Path) -> None:
    """Set the JSON log file path (overrides ``GYM_LOG_PATH``)."""
    global _explicit_path
    _explicit_path = Path(path).expanduser().resolve()


def _resolved_log_path() -> Path:
    if _explicit_path is not None:
        return _explicit_path
    env = os.environ.get("GYM_LOG_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd() / _DEFAULT_FILENAME


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": _LOG_VERSION, "entries": []}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {"version": _LOG_VERSION, "entries": []}
    data = json.loads(text)
    if isinstance(data, list):
        return {"version": _LOG_VERSION, "entries": data}
    if not isinstance(data, dict):
        raise ValueError("gym_log.json must be an object or array")
    entries = data.get("entries")
    if entries is None:
        raise ValueError("gym_log.json missing 'entries'")
    if not isinstance(entries, list):
        raise ValueError("gym_log.json 'entries' must be a list")
    ver = data.get("version", _LOG_VERSION)
    if not isinstance(ver, int):
        ver = _LOG_VERSION
    return {"version": ver, "entries": entries}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=".gym_log_", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _last_ground_truth(entries: list[dict[str, Any]]) -> float | None:
    for e in reversed(entries):
        gt = e.get("ground_truth_accuracy")
        if gt is not None:
            try:
                return float(gt)
            except (TypeError, ValueError):
                continue
    return None


def log_entry(
    *,
    entry_type: EntryType,
    change_summary: str,
    models_used: list[str],
    per_model_accuracy: dict[str, float],
    ensemble_accuracy: float | None = None,
    ground_truth_accuracy: float | None = None,
    notes: str | None = None,
    log_path: str | Path | None = None,
    provenance: dict[str, Any] | None = None,
    experiment_log_entry_id: str | None = None,
) -> dict[str, Any]:
    """Append one log entry and persist to ``gym_log.json``.

    ``delta_from_last_submission`` is set only for submissions with a
    ground-truth score and a prior submission that also had ground truth.

    If ``log_path`` is set, that file is used for this call only (does not
    change the global path from ``set_log_path`` / ``GYM_LOG_PATH`` / cwd).
    """
    path = Path(log_path).expanduser().resolve() if log_path else _resolved_log_path()
    data = _load_raw(path)
    entries: list[dict[str, Any]] = data["entries"]

    delta: float | None = None
    if entry_type == "submission" and ground_truth_accuracy is not None:
        prev = _last_ground_truth(entries)
        if prev is not None:
            delta = float(ground_truth_accuracy) - prev

    prov: dict[str, Any] = {}
    if provenance:
        prov.update(provenance)
    if experiment_log_entry_id:
        prov["experiment_log_entry_id"] = experiment_log_entry_id

    row: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry_type": entry_type,
        "change_summary": change_summary,
        "models_used": list(models_used),
        "per_model_accuracy": dict(per_model_accuracy),
        "ensemble_accuracy": ensemble_accuracy,
        "ground_truth_accuracy": ground_truth_accuracy,
        "delta_from_last_submission": delta,
        "notes": notes,
    }
    if prov:
        row["provenance"] = prov
    entries.append(row)
    data["entries"] = entries
    data["version"] = _LOG_VERSION
    _atomic_write(path, data)
    return row


def _format_cv_acc(entry: dict[str, Any]) -> str:
    ens = entry.get("ensemble_accuracy")
    if ens is not None:
        try:
            return f"{float(ens):.4f}"
        except (TypeError, ValueError):
            return "—"
    per = entry.get("per_model_accuracy") or {}
    if not isinstance(per, dict) or not per:
        return "—"
    vals: list[float] = []
    for v in per.values():
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return "—"
    if len(vals) == 1:
        return f"{vals[0]:.4f}"
    return f"best:{max(vals):.2f}"


def _format_gt(entry: dict[str, Any]) -> str:
    gt = entry.get("ground_truth_accuracy")
    if gt is None:
        return "—"
    try:
        return f"{float(gt):.4f}"
    except (TypeError, ValueError):
        return "—"


def _format_delta(entry: dict[str, Any]) -> str:
    d = entry.get("delta_from_last_submission")
    if d is None:
        return "—"
    try:
        x = float(d)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.4f}"


def _format_time(iso_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return local.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_ts[:16] if iso_ts else "—"


def _format_git_short(entry: dict[str, Any]) -> str:
    prov = entry.get("provenance")
    if isinstance(prov, dict):
        s = prov.get("revision_short")
        if s:
            return str(s)[:8]
    return "—"


def _truncate(s: str, width: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= width:
        return s
    if width <= 3:
        return s[:width]
    return s[: width - 3] + "..."


def print_summary(log_path: str | Path | None = None) -> None:
    """Print a fixed-width table of all log entries.

    If ``log_path`` is set, read that file instead of the configured default.
    """
    path = Path(log_path).expanduser().resolve() if log_path else _resolved_log_path()
    data = _load_raw(path)
    entries: list[dict[str, Any]] = data["entries"]

    w_idx, w_time, w_type, w_change, w_models, w_cv, w_gt, w_delta, w_git = (
        3,
        11,
        10,
        28,
        14,
        8,
        8,
        8,
        8,
    )
    header = (
        f"{'#':>{w_idx}} | "
        f"{'Time':<{w_time}} | "
        f"{'Type':<{w_type}} | "
        f"{'Change':<{w_change}} | "
        f"{'Models':<{w_models}} | "
        f"{'CV Acc':>{w_cv}} | "
        f"{'GT Acc':>{w_gt}} | "
        f"{'Δ':>{w_delta}} | "
        f"{'Git':>{w_git}}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)
    if not entries:
        print("(no entries)")
        print(sep)
        return

    for i, e in enumerate(entries, start=1):
        models = e.get("models_used") or []
        if isinstance(models, list):
            models_str = ", ".join(str(m) for m in models)
        else:
            models_str = str(models)
        line = (
            f"{i:>{w_idx}} | "
            f"{_format_time(str(e.get('timestamp', ''))):<{w_time}} | "
            f"{str(e.get('entry_type', '')):<{w_type}} | "
            f"{_truncate(str(e.get('change_summary', '')), w_change):<{w_change}} | "
            f"{_truncate(models_str, w_models):<{w_models}} | "
            f"{_format_cv_acc(e):>{w_cv}} | "
            f"{_format_gt(e):>{w_gt}} | "
            f"{_format_delta(e):>{w_delta}} | "
            f"{_format_git_short(e):>{w_git}}"
        )
        print(line)
    print(sep)
