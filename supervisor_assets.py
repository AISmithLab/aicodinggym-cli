"""Seed supervisor assets (``supervisor.sh``, ``dashboard.html``, ``tools/notebook_metrics.py``)
into a problem folder.

The canonical templates live in ``aicodinggym/templates/``. They are shipped as
package-data so this module works whether the CLI was installed from PyPI or
as an editable local install. This module is only a **local fallback** for
:func:`aicodinggym.cli._install_gym_environment`, which normally pulls the
same files from the ``test`` branch of ``AICodingGym/gym-environment``.

If you change the supervisor or dashboard:
  1. Edit ``gym-environment/supervisor.sh`` or ``gym-environment/dashboard.html``
     (the canonical copies, also published to GitHub).
  2. Re-copy into ``aicodinggym-cli/templates/`` (the CLI's vendored fallback).
  3. Keep the two ``DEFAULT_CMD``/``SUBMIT_CMD`` lines in the .template
     replaced with ``__DEFAULT_CMD__`` / ``__SUBMIT_CMD__`` markers.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path


_TEMPLATE_PKG = "aicodinggym.templates"


def _read_template(name: str) -> str:
    """Read a template file shipped inside the ``aicodinggym.templates`` package.

    Works for both editable and installed distributions. Falls back to reading
    directly from the on-disk ``templates/`` directory next to this module, in
    case the package is being used before it has been installed (e.g. during
    development without ``pip install -e .``).
    """
    try:
        return resources.files(_TEMPLATE_PKG).joinpath(name).read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError):
        # Fallback: sibling ``templates`` directory.
        local = Path(__file__).with_name("templates") / name
        return local.read_text(encoding="utf-8")


def _defaults(problem_id: str, challenge: str) -> tuple[str, str]:
    """Return ``(DEFAULT_CMD, SUBMIT_CMD)`` strings injected into ``supervisor.sh``.

    ``DEFAULT_CMD`` runs when the user invokes ``./supervisor.sh --cmd`` without
    an argument; ``SUBMIT_CMD`` runs on ``./supervisor.sh --submit``.
    """
    if challenge == "mle":
        return (
            f"aicodinggym mle log show {problem_id}",
            f"aicodinggym mle submit {problem_id} -F submission.csv",
        )
    if challenge == "swe":
        return (
            f"aicodinggym swe test {problem_id}",
            f"aicodinggym swe submit {problem_id}",
        )
    if challenge == "cr":
        return (
            "git status",
            f"aicodinggym cr submit {problem_id} -f review.md",
        )
    return ("echo ready", "echo submit")


def ensure_supervisor_assets(problem_dir: Path, problem_id: str, challenge: str) -> None:
    """Create ``supervisor.sh``, ``dashboard.html`` and ``tools/notebook_metrics.py``
    inside *problem_dir* if any are missing.

    Each asset is written with a POSIX ``\\n`` line ending and executable-friendly
    encoding. Files that already exist are left untouched so user customizations
    (e.g. tweaks to the dashboard) are preserved across re-runs.
    """
    problem_dir = Path(problem_dir)
    problem_dir.mkdir(parents=True, exist_ok=True)
    tools_dir = problem_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)

    helper_path = tools_dir / "notebook_metrics.py"
    if not helper_path.exists():
        helper_path.write_text(_read_template("notebook_metrics.py"), encoding="utf-8", newline="\n")

    approach_path = tools_dir / "summarize_approach.py"
    if not approach_path.exists():
        approach_path.write_text(_read_template("summarize_approach.py"), encoding="utf-8", newline="\n")

    supervisor_path = problem_dir / "supervisor.sh"
    if not supervisor_path.exists():
        default_cmd, submit_cmd = _defaults(problem_id, challenge)
        body = (
            _read_template("supervisor.sh.template")
            .replace("__DEFAULT_CMD__", default_cmd)
            .replace("__SUBMIT_CMD__", submit_cmd)
        )
        supervisor_path.write_text(body, encoding="utf-8", newline="\n")
        try:
            supervisor_path.chmod(0o755)
        except OSError:
            # Windows / read-only filesystems: not fatal.
            pass

    dashboard_path = problem_dir / "dashboard.html"
    if not dashboard_path.exists():
        dashboard_path.write_text(_read_template("dashboard.html"), encoding="utf-8", newline="\n")
