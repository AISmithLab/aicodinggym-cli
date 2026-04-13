"""AI Coding Gym CLI.

Imports are lazy so tooling that loads this file without package context
(e.g. some pytest collection paths) does not fail on relative imports.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.5.1"

__all__ = [
    "__version__",
    "ExperimentLog",
    "LogEntry",
    "capture_mle_provenance",
    "log_entry",
    "print_summary",
    "set_log_path",
    "gym_logger",
]


def __getattr__(name: str) -> Any:
    if name in ("ExperimentLog", "LogEntry", "capture_mle_provenance"):
        m = importlib.import_module("aicodinggym.experiment_log")
        return getattr(m, name)
    if name in ("log_entry", "print_summary", "set_log_path"):
        m = importlib.import_module("aicodinggym.gym_logger")
        return getattr(m, name)
    if name == "gym_logger":
        return importlib.import_module("aicodinggym.gym_logger")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:
    from .experiment_log import ExperimentLog, LogEntry, capture_mle_provenance
    from .gym_logger import log_entry, print_summary, set_log_path
