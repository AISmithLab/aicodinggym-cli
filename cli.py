"""AI Coding Gym CLI - main entry point.

A command-line tool for the AI Coding Gym platform (https://aicodinggym.com).
Supports SWE-bench, MLE-bench, and Code Review challenges.

SETUP (required before any other command):
    aicodinggym configure --user-id YOUR_USER_ID

SWE-BENCH WORKFLOW:
    aicodinggym swe fetch django__django-10097
    # ... edit code to fix the issue ...
    aicodinggym swe submit django__django-10097

MLE-BENCH WORKFLOW:
    aicodinggym mle download spaceship-titanic
    # ... train model, generate predictions ...
    aicodinggym mle submit spaceship-titanic -F submission.csv

CODE REVIEW WORKFLOW:
    aicodinggym cr fetch sentry-0001
    # ... review the diff and write your review ...
    aicodinggym cr submit sentry-0001 -f review.md
"""

import json
import os
import platform
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import click

from . import __version__
from . import entire_logging
from .api import (
    APIError,
    configure as api_configure,
    cr_submit_review,
    fetch_pr as api_fetch_pr,
    fetch_problem as api_fetch_problem,
    mlebench_download_file,
    mlebench_download_open,
    mlebench_submit_csv,
    submit_notification,
)
from .config import (
    get_logging_consent,
    load_config,
    load_credentials,
    save_config,
    save_credentials,
    set_logging_consent,
)
from .git_ops import (
    add_commit_push,
    check_tool_installed,
    clone_repo,
    clone_repo_cr,
    generate_ssh_key_pair,
    reset_to_setup_commit,
    restore_branch,
    run_git_command,
)


def _hyperlink(url: str, text: str | None = None) -> str:
    """Return an OSC 8 terminal hyperlink. Falls back to plain URL on unsupported terminals."""
    label = text or url
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def _error(msg: str) -> None:
    """Print an error message to stderr and exit."""
    click.echo(f"Error: {msg}", err=True)
    sys.exit(1)


def _warn(msg: str) -> None:
    """Print a warning message to stderr."""
    click.echo(f"Warning: {msg}", err=True)


_GYM_ENV_API = "https://api.github.com/repos/AICodingGym/gym-environment/contents"
_GYM_ENV_SKIP = {"README.md"}


def _download_directory(api_url: str, dest_dir: Path) -> None:
    """Recursively download all files from a GitHub API directory listing."""
    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            entries = json.loads(r.read())
    except Exception as e:
        _warn(f"Failed to list directory {dest_dir.name}: {e}")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        name = entry.get("name", "")
        etype = entry.get("type")
        if etype == "file":
            url = entry.get("download_url")
            if not url:
                continue
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    (dest_dir / name).write_bytes(r.read())
            except Exception as e:
                _warn(f"Failed to download {dest_dir.name}/{name}: {e}")
        elif etype == "dir":
            sub_url = entry.get("url")
            if sub_url:
                _download_directory(sub_url, dest_dir / name)


def _install_gym_environment(dest: Path) -> None:
    """Download gym-environment files into dest and add them to .gitignore."""
    try:
        req = urllib.request.Request(_GYM_ENV_API, headers={"Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            entries = json.loads(resp.read())
    except Exception as e:
        _warn(f"Could not fetch gym-environment file list: {e}")
        return

    downloaded: list[str] = []

    for entry in entries:
        name = entry.get("name", "")
        if name in _GYM_ENV_SKIP:
            continue
        etype = entry.get("type")

        if etype == "file":
            url = entry.get("download_url")
            if not url:
                continue
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    (dest / name).write_bytes(r.read())
                downloaded.append(name)
            except Exception as e:
                _warn(f"Failed to download {name}: {e}")

        elif etype == "dir":
            sub_url = entry.get("url")
            if sub_url:
                _download_directory(sub_url, dest / name)
            downloaded.append(name)

    if not downloaded:
        return

    # Append to .gitignore
    gitignore = dest / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    existing_lines = set(existing.splitlines())
    new_entries = [f for f in downloaded if f not in existing_lines and f"/{f}" not in existing_lines]
    if new_entries:
        block = "\n# gym-environment\n" + "\n".join(new_entries) + "\n"
        with open(gitignore, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(block)



def _resolve_user_id(config: dict, user_id: str | None) -> str:
    """Resolve user_id from argument or config, with helpful error."""
    if user_id:
        return user_id
    uid = config.get("user_id")
    if not uid:
        _error(
            "User ID is not configured.\n\n"
            "Run 'aicodinggym configure --user-id YOUR_USER_ID' first.\n"
            "This generates an SSH key and registers it with aicodinggym.com."
        )
    return uid


def _resolve_workspace(config: dict, workspace_dir: str | None) -> Path:
    """Resolve workspace directory from argument or config."""
    if workspace_dir:
        return Path(workspace_dir).resolve()
    configured = config.get("workspace_dir")
    if configured:
        return Path(configured).resolve()
    return Path.cwd().resolve()


def _print_test_summary(lines: list[str], problem_id: str, returncode: int,
                        elapsed: float = 0.0) -> None:
    """Parse act output and print a clear test results summary."""

    steps: list[tuple[str, str]] = []  # (status, name)
    test_results: list[str] = []
    failures: list[str] = []
    errors: list[str] = []
    ran_line = ""

    for raw in lines:
        line = raw.rstrip()

        # Capture step results (Success/Failure lines)
        m = re.search(r"(✅\s+Success|❌\s+Failure)\s+-\s+(.+?)(?:\s+\[.*\])?$", line)
        if m:
            status = "PASS" if "Success" in m.group(1) else "FAIL"
            steps.append((status, m.group(2).strip()))

        # Capture individual test results (PASS/FAIL/ERROR/ok lines)
        test_m = re.search(r"\|\s+([\w_]+\s+\([\w.]+\))\s+\.\.\.\s+(ok|FAIL|ERROR)", line)
        if test_m:
            test_results.append(f"  {test_m.group(2):>5}  {test_m.group(1)}")

        # Capture "Ran N tests" line
        ran_m = re.search(r"Ran (\d+) tests? in (.+)", line)
        if ran_m:
            ran_line = ran_m.group(0)

        # Capture FAIL/ERROR blocks
        fail_m = re.search(r"\|\s+(FAIL|ERROR): (.+)", line)
        if fail_m:
            if fail_m.group(1) == "FAIL":
                failures.append(fail_m.group(2))
            else:
                errors.append(fail_m.group(2))

    click.echo("\n" + "=" * 60)
    click.echo(f"  TEST SUMMARY — {problem_id}")
    click.echo("=" * 60)

    if steps:
        click.echo("\nWorkflow steps:")
        for status, name in steps:
            icon = "PASS" if status == "PASS" else "FAIL"
            click.echo(f"  [{icon}] {name}")

    if test_results:
        click.echo("\nTest results:")
        for tr in test_results:
            click.echo(tr)

    if failures:
        click.echo(f"\nFailed tests ({len(failures)}):")
        for f in failures:
            click.echo(f"  - {f}")

    if errors:
        click.echo(f"\nErrored tests ({len(errors)}):")
        for e in errors:
            click.echo(f"  - {e}")

    if ran_line:
        click.echo(f"\n{ran_line}")

    if returncode == 0:
        click.echo(f"\nResult: ALL TESTS PASSED")
    else:
        click.echo(f"\nResult: TESTS FAILED (exit code {returncode})")

    if elapsed > 0:
        minutes, seconds = divmod(int(elapsed), 60)
        click.echo(f"Elapsed: {minutes}m {seconds}s")

    click.echo("=" * 60)


def _ensure_act_config() -> None:
    """Create act config file with medium image if it doesn't exist.

    Prevents act from prompting interactively on first run.
    """
    if sys.platform == "win32":
        actrc = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "act" / "actrc"
    elif sys.platform == "darwin":
        actrc = Path.home() / "Library" / "Application Support" / "act" / "actrc"
    else:
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        actrc = xdg / "act" / "actrc"

    if actrc.exists():
        return

    actrc.parent.mkdir(parents=True, exist_ok=True)
    # Medium image: ~500MB, compatible with most actions
    actrc.write_text(
        "-P ubuntu-latest=catthehacker/ubuntu:act-latest\n"
        "-P ubuntu-22.04=catthehacker/ubuntu:act-22.04\n"
        "-P ubuntu-20.04=catthehacker/ubuntu:act-20.04\n"
    )
    click.echo(f"Created act config at {actrc} (using medium runner images).")


def _resolve_key_path(config: dict, creds: dict | None = None) -> Path:
    """Resolve SSH private key path from credentials or config."""
    path_str = None
    if creds:
        path_str = creds.get("private_key_path")
    if not path_str:
        path_str = config.get("private_key_path")
    if not path_str:
        _error(
            "SSH key path not found.\n\n"
            "Run 'aicodinggym configure --user-id YOUR_USER_ID' to generate a key.\n"
            "If you previously configured, your config may be corrupted.\n"
            "Config location: ~/.aicodinggym/config.json"
        )
    key_path = Path(path_str)
    if not key_path.exists():
        _error(
            f"SSH key file not found at: {key_path}\n\n"
            "Run 'aicodinggym configure --user-id YOUR_USER_ID' to regenerate.\n"
            "This will create a new SSH key pair and register it with the server."
        )
    return key_path


# ── AI-session logging (Entire integration) ──────────────────────────────────

_CONSENT_PROMPT = (
    "AI Coding Gym can upload this AI coding session (prompts, responses, files "
    "changed) for research only. Data is de-identified/anonymized before use.\n"
    "Upload your session logs on submit?"
)


def _configure_hint(user_id: str | None) -> str:
    """A copy-pasteable 'configure' command (where Entire install is offered)."""
    return f"aicodinggym configure --user-id {user_id}" if user_id else "aicodinggym configure"


def _setup_logging(problem_dir: Path, *, init_git: bool = False,
                   user_id: str | None = None) -> bool:
    """Best-effort: install Entire hooks so the session is captured locally.

    Capture is local-only; nothing is uploaded until the user consents at
    submit. If Entire isn't installed, point the user at ``configure`` (which
    offers to install it) rather than silently skipping — unless they've already
    opted out of logging. Returns True if AI-session capture is now active.
    """
    if not entire_logging.is_available():
        if get_logging_consent() is not False:  # not explicitly opted out
            click.echo(
                "  Logging:     Not set up — the 'entire' CLI isn't installed.\n"
                f"               Run '{_configure_hint(user_id)}' to enable AI workflow logging."
            )
        return False
    ok, msg = entire_logging.setup(problem_dir, init_git=init_git)
    if ok:
        click.echo(f"  Logging:     {msg} (uploaded only with your consent on submit)")
        return True
    # On setup failure (Entire present but enable errored) we stay quiet.
    return False


def _launch_instruction(problem_dir: Path) -> str:
    """Reminder that AI-session capture only works for an agent launched here.

    Claude Code (and Codex etc.) load their capture hooks from the directory
    they're started in, fixed for the whole session — cd-ing in later does not
    activate them. So the agent must be launched inside the problem folder.
    """
    return (
        f"Please start your agent inside {problem_dir}:\n"
        f"  cd {problem_dir}\n"
        "  claude                # or codex / your AI agent\n"
        "(Capture only works for an agent launched here — not one you cd into later.)"
    )


def _safe_key_path(config: dict, creds: dict | None = None) -> Path | None:
    """Resolve the SSH key for a log push without exiting if it's missing.

    Unlike :func:`_resolve_key_path`, this returns None instead of aborting, so
    an optional log upload never kills a submit that already succeeded.
    """
    path_str = (creds or {}).get("private_key_path") or config.get("private_key_path")
    if not path_str:
        return None
    key_path = Path(path_str)
    return key_path if key_path.exists() else None


def _resolve_log_upload_consent(flag: bool | None) -> bool:
    """Resolve whether to upload session logs, prompting once if needed.

    Precedence: explicit --upload-logs/--no-upload-logs flag > stored consent >
    first-time prompt. In non-interactive sessions with no recorded choice we
    default to NOT uploading (privacy-safe) and print how to opt in.
    """
    if flag is not None:
        set_logging_consent(flag)
        return flag
    stored = get_logging_consent()
    if stored is not None:
        return stored
    if not sys.stdin.isatty():
        click.echo(
            "AI session captured locally; upload skipped (no consent on record).\n"
            "  Opt in for research (de-identified): aicodinggym configure --upload-logs"
        )
        return False
    answer = click.confirm("\n" + _CONSENT_PROMPT, default=False)
    set_logging_consent(answer)
    return answer


def _resolve_logs_remote(benchmark: str, creds: dict | None,
                         config: dict, override: str | None) -> str | None:
    """Resolve the writable git URL to push session logs to.

    All benchmarks log to the user's single repo (one repo, many branches),
    distinguished by the per-problem log branch name. SWE may fall back to its
    own writable clone URL when the submission repo isn't recorded yet; CR's
    cloned repo is the read-only PR and must never be used.
    """
    if override:
        return override
    env = os.environ.get("AICODINGGYM_LOGS_REMOTE")
    if env:
        return env
    if config.get("submission_repo_url"):
        return config["submission_repo_url"]
    if benchmark == "swe":
        return (creds or {}).get("repo_url")
    return None


def _logging_status(thunk) -> str:
    """Run a logging step before the success banner, never suppressing it.

    The submission already succeeded by the time logging runs, so any unexpected
    error here must not hide the banner — degrade to a warning + empty status.
    """
    try:
        return thunk()
    except Exception as e:  # noqa: BLE001
        _warn(f"AI session logging skipped due to an error: {e}")
        return ""


def _maybe_upload_logs(problem_dir: Path, *, benchmark: str, problem_id: str,
                       user_id: str, key_path: Path | None, config: dict,
                       creds: dict | None, upload_flag: bool | None,
                       logs_remote_override: str | None, tool: str | None = None,
                       flush: bool = False) -> str:
    """Consent-gated upload of the captured AI session at submit time.

    Returns a status line (or "") to embed in the caller's success summary. Any
    interactive consent prompt happens here, so callers should invoke this
    *before* printing their banner. Hard failures are warned to stderr.
    """
    if not entire_logging.is_available():
        if get_logging_consent() is not False:  # not explicitly opted out
            return (
                "  Logs:    Not captured — the 'entire' CLI isn't installed.\n"
                f"           Run '{_configure_hint(user_id)}' to enable logging next time."
            )
        return ""
    if not entire_logging.is_enabled(problem_dir):
        return ""  # repo wasn't set up for capture (e.g. fetched before install)
    if flush:
        entire_logging.flush(problem_dir)
    if not entire_logging.has_sessions(problem_dir):
        return ""  # nothing was captured — stay quiet

    if not _resolve_log_upload_consent(upload_flag):
        return "  Logs:    AI session captured locally; upload skipped."

    remote = _resolve_logs_remote(benchmark, creds, config, logs_remote_override)
    if not remote:
        return (
            "  Logs:    consented, but no logs repository is configured.\n"
            "           Re-run 'aicodinggym configure' or pass --logs-remote URL."
        )

    ok, info = entire_logging.upload(
        problem_dir, remote_url=remote, benchmark=benchmark, problem_id=problem_id,
        user_id=user_id, key_path=key_path, tool=tool, cli_version=__version__,
    )
    if ok:
        return f"  Logs:    uploaded for research (branch {info})"
    _warn(f"AI session log upload failed: {info}")
    return ""


def _maybe_submit_mle_artifacts(workspace_repo: Path, *, competition_id: str,
                                user_id: str, config: dict, key_path: Path | None,
                                upload_flag: bool | None,
                                logs_remote_override: str | None) -> str:
    """MLE submit: always push the user's solution code to their submission repo,
    and — only with consent — also upload the captured AI session logs.

    Pushing the code is the whole point of ``submit`` and it goes to the user's
    *own* repo, so it is NOT gated on research-log consent; only the
    de-identified AI session upload is. The code lands on a stable branch named
    after the competition (overwritten each submit, so ``mle restore`` has a
    predictable name to pull); logs land on a unique per-submission branch so
    prior sessions are never overwritten. The CSV itself already went to the
    API. Returns a status block (or "").
    """
    workspace_repo = Path(workspace_repo)
    if not (workspace_repo / ".git").exists():
        return ""  # workspace was never initialised (e.g. downloaded pre-upgrade)

    # Commit the solution code locally. With Entire enabled, this commit also
    # captures the AI session checkpoint (commit_linking is always on) — no
    # separate flush needed. The session is uploaded only with consent, below.
    entire_logging.commit_workspace(workspace_repo, f"MLE submission: {competition_id}")

    remote = _resolve_logs_remote("mle", None, config, logs_remote_override)
    if not remote:
        return (
            "  Code:    committed locally, but no submission repository is configured.\n"
            "           Re-run 'aicodinggym configure' or pass --logs-remote URL."
        )

    lines: list[str] = []

    # 1) Always push the solution code — it's the user's own repo and the point
    #    of submitting; independent of research-log consent.
    code_ok, code_info = entire_logging.push_branch(
        workspace_repo, remote_url=remote, dest_branch=competition_id,
        key_path=key_path, force=True,
    )
    if code_ok:
        lines.append(f"  Code:    pushed to branch '{code_info}'")
        lines.append(f"           Restore later with: aicodinggym mle restore {competition_id}")
    else:
        _warn(f"MLE code push failed: {code_info}")

    # 2) Upload the de-identified AI session logs only with consent.
    if not entire_logging.is_available():
        lines.append("  Logs:    AI session not captured ('entire' not installed).")
        lines.append(f"           Run '{_configure_hint(user_id)}' to enable logging next time.")
    elif not (entire_logging.is_enabled(workspace_repo)
              and entire_logging.has_sessions(workspace_repo)):
        pass  # no AI session was captured — stay quiet
    elif not _resolve_log_upload_consent(upload_flag):
        lines.append("  Logs:    AI session captured locally; upload skipped.")
    else:
        ok, info = entire_logging.upload(
            workspace_repo, remote_url=remote, benchmark="mle", problem_id=competition_id,
            user_id=user_id, key_path=key_path, cli_version=__version__,
            submission_stamp=entire_logging.new_stamp(),
        )
        if ok:
            lines.append(f"  Logs:    uploaded for research (branch {info})")
        else:
            _warn(f"AI session log upload failed: {info}")

    return "\n".join(lines)


def _configure_logging(upload_logs_flag: bool | None) -> None:
    """During configure: offer to install Entire, then record upload consent.

    Consent is resolved here — where a human is reliably at the keyboard —
    rather than only at submit. Later submits are often non-interactive (e.g.
    driven by an AI agent, where stdin is not a TTY); recording the choice now
    gives them an answer to act on instead of silently skipping the upload.
    """
    if entire_logging.is_available():
        ver = entire_logging.version()
        click.echo(f"  Logging:     Entire detected ({ver or 'installed'})")
    else:
        click.echo(
            "\nOptional — AI workflow logging:\n"
            "  AI Coding Gym can capture your AI coding sessions (via Entire,\n"
            "  https://entire.io) and, only with your consent, upload them for\n"
            "  research. Uploaded data is de-identified/anonymized. Needs the\n"
            "  'entire' CLI."
        )
        if not sys.stdin.isatty():
            click.echo(f"  Install later with:\n    {entire_logging.INSTALL_COMMAND}")
        elif click.confirm("Install the Entire CLI now?", default=True):
            click.echo("Installing Entire (downloading a binary; this may take a minute)...")
            ok, msg = entire_logging.install()
            if ok:
                click.echo(f"  Entire: {msg}")
            else:
                _warn(
                    f"Could not install Entire automatically: {msg}\n"
                    f"  Install manually: {entire_logging.INSTALL_COMMAND}"
                )
        else:
            click.echo(f"  Skipped. Install later with:\n    {entire_logging.INSTALL_COMMAND}")

    # Record the standing upload consent now. An explicit flag wins; otherwise
    # ask once (only with a TTY and no prior choice) so non-interactive submits
    # have an answer. The submit-time prompt remains as a fallback for anyone who
    # configured non-interactively.
    if upload_logs_flag is not None:
        set_logging_consent(upload_logs_flag)
    elif get_logging_consent() is None and sys.stdin.isatty():
        granted = click.confirm("\n" + _CONSENT_PROMPT, default=False)
        set_logging_consent(granted)
        click.echo(
            "  Consent recorded — change it anytime with\n"
            "    'aicodinggym configure --upload-logs'  (or --no-upload-logs)."
        )


# ── Top-level group ──────────────────────────────────────────────────────────


@click.group(
    epilog=(
        "\b\n"
        "SETUP (run once before using other commands):\n"
        "  aicodinggym configure --user-id YOUR_USER_ID\n"
        "  (user_id is required — get yours at https://aicodinggym.com)\n\n"
        "\b\n"
        "EXAMPLES:\n"
        "  aicodinggym swe fetch django__django-10097\n"
        "  aicodinggym swe submit django__django-10097 --message 'Fix auth bug'\n"
        "  aicodinggym mle download spaceship-titanic\n"
        "  aicodinggym mle submit spaceship-titanic -F predictions.csv\n"
        "  aicodinggym cr fetch sentry-0001\n"
        "  aicodinggym cr submit sentry-0001 -f review.md\n\n"
        "\b\n"
        "WEBSITE:\n"
        "  https://aicodinggym.com\n"
    ),
)
@click.version_option(__version__, prog_name="aicodinggym")
def main():
    """AI Coding Gym CLI.

    A command-line interface for the AI Coding Gym platform
    (https://aicodinggym.com). Provides tools to fetch coding problems,
    download datasets, and submit solutions.

    Designed for use by both humans and LLM/AI agents.

    \b
    QUICK START:
      1. Configure:  aicodinggym configure --user-id YOUR_USER_ID
      2. Fetch:      aicodinggym swe fetch PROBLEM_ID
      3. Solve:      (edit code in the cloned repository)
      4. Submit:     aicodinggym swe submit PROBLEM_ID
    """
    pass


# ── configure ────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--user-id", required=True,
    help="Your AI Coding Gym user ID. Get one at https://aicodinggym.com.",
)
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Default workspace directory for cloning repositories. "
         "Defaults to the current working directory.",
)
@click.option(
    "--upload-logs/--no-upload-logs", "upload_logs", default=None,
    help="Pre-set consent for uploading de-identified AI session logs for "
         "research (otherwise you're asked once, on your first submit).",
)
def configure(user_id: str, workspace_dir: str | None, upload_logs: bool | None):
    """Configure credentials and register SSH key with aicodinggym.com.

    Generates an SSH key pair locally (stored in ~/.aicodinggym/),
    sends the public key to the server, and saves your configuration.

    \b
    This command must be run once before using any other commands.
    If you've already configured, running again will reuse your existing key.

    \b
    WHAT IT DOES:
      1. Generates SSH key pair in ~/.aicodinggym/
      2. Registers your public key with the AI Coding Gym server
      3. Receives your assigned repository name
      4. Saves all settings to ~/.aicodinggym/config.json

    \b
    EXAMPLE:
      aicodinggym configure --user-id alice123
      aicodinggym configure --user-id alice123 --workspace-dir ~/gym-workspace
    """
    try:
        click.echo(f"Generating SSH key for user '{user_id}'...")
        private_key_path, public_key = generate_ssh_key_pair(user_id)

        click.echo("Registering public key with aicodinggym.com...")
        existing = load_config()
        submission_repo_url = existing.get("submission_repo_url")
        try:
            data = api_configure(user_id, public_key)
            repo_name = data.get("repo_name")
            if not repo_name:
                _error("Server did not return a repository name. Please try again or contact support.")
            # Writable repo we can push AI-session logs to (CR/MLE upload target).
            submission_repo_url = data.get("repo_url") or submission_repo_url
        except APIError as e:
            if "409" in str(e):
                click.echo("Key already registered, reusing existing configuration.")
                repo_name = existing.get("repo_name", f"submission-{user_id}")
            else:
                raise

        # Only change the workspace when --workspace-dir is explicitly given.
        # On re-configure, preserve the previously configured workspace instead
        # of silently relocating it to wherever 'configure' happened to run;
        # fall back to the current directory only on first-time setup.
        if workspace_dir:
            resolved_workspace = str(Path(workspace_dir).resolve())
        else:
            resolved_workspace = existing.get("workspace_dir") or str(Path.cwd().resolve())

        config = {
            "user_id": user_id,
            "repo_name": repo_name,
            "private_key_path": str(private_key_path),
            "workspace_dir": resolved_workspace,
        }
        if submission_repo_url:
            config["submission_repo_url"] = submission_repo_url
        save_config(config)

        _install_gym_environment(Path(resolved_workspace))

        click.echo(
            f"\nConfiguration saved successfully!\n"
            f"\n"
            f"  User ID:     {user_id}\n"
            f"  Repository:  {repo_name}\n"
            f"  Workspace:   {resolved_workspace}\n"
            f"  SSH Key:     {private_key_path}\n"
            f"  Config:      ~/.aicodinggym/config.json"
        )

        _configure_logging(upload_logs)

        click.echo(
            "\nYou can now use 'aicodinggym swe', 'aicodinggym mle', and 'aicodinggym cr' commands."
        )
    except APIError as e:
        _error(str(e))
    except Exception as e:
        _error(f"Configuration failed: {e}")


# ── swe group ────────────────────────────────────────────────────────────────


@main.group()
def swe():
    """SWE-bench coding challenges - fetch, solve, and submit bug fixes.

    \b
    PREREQUISITE:
      Run 'aicodinggym configure --user-id YOUR_USER_ID' before using these commands.

    \b
    WORKFLOW:
      1. aicodinggym swe fetch PROBLEM_ID     # Clone the problem repo
      2. (edit code to fix the issue)          # Work on your solution
      3. aicodinggym swe submit PROBLEM_ID     # Submit your fix
      4. aicodinggym swe reset PROBLEM_ID      # (optional) Start over

    \b
    PROBLEM IDS:
      Problem IDs follow the format: <project>__<repo>-<number>
      Examples: django__django-10097, sympy__sympy-13043, scikit-learn__scikit-learn-11578
    """
    pass


@swe.command("fetch")
@click.argument("problem_id")
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Directory to clone into. Overrides configured workspace.",
)
def swe_fetch(problem_id: str, user_id: str | None, workspace_dir: str | None):
    """Fetch a SWE-bench problem and clone its repository locally.

    Contacts the AI Coding Gym server to set up the problem branch,
    then clones the repository into your workspace directory.

    \b
    PREREQUISITE:
      You must run 'aicodinggym configure --user-id YOUR_USER_ID' first.
      If you haven't configured yet, this command will fail with instructions.

    \b
    ARGUMENTS:
      PROBLEM_ID  The unique problem identifier (e.g., 'django__django-10097').
                  Get problem IDs from https://aicodinggym.com.

    \b
    WHAT IT DOES:
      1. Requests the problem branch from the server
      2. Clones the repository (shallow clone for efficiency)
      3. Sets up your local workspace at <workspace>/<problem_id>/

    \b
    EXAMPLE:
      aicodinggym swe fetch django__django-10097
      aicodinggym swe fetch django__django-10097 --workspace-dir ~/projects
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)
    workspace = _resolve_workspace(config, workspace_dir)
    key_path = _resolve_key_path(config)

    try:
        click.echo(f"Fetching problem '{problem_id}' from server...")
        data = api_fetch_problem(uid, problem_id)
    except APIError as e:
        _error(str(e))

    branch = data.get("branch_name", problem_id)
    repo_url = data.get("repo_url")
    server_msg = data.get("message", "")

    if not repo_url:
        _error("Server did not return a repository URL. The problem may not exist.")

    # Save credentials for later submit
    credentials = load_credentials()
    credentials[problem_id] = {
        "repo_url": repo_url,
        "branch": branch,
        "user_id": uid,
        "private_key_path": str(key_path),
        "workspace_dir": str(workspace),
        "benchmark": "swe",
    }
    save_credentials(credentials)

    workspace.mkdir(parents=True, exist_ok=True)

    click.echo(f"Cloning branch '{branch}' into {workspace / problem_id}...")
    success, msg = clone_repo(repo_url, branch, problem_id, str(workspace), key_path)

    if not success:
        _error(msg)

    # Record the user's writable repo (one repo, many branches) so CR/MLE logs
    # can target it too. Only set if not already configured.
    if repo_url and not config.get("submission_repo_url"):
        config["submission_repo_url"] = repo_url
        save_config(config)

    _install_gym_environment(workspace / problem_id)
    capture_active = _setup_logging(workspace / problem_id, user_id=uid)

    click.echo(
        f"\nSuccessfully fetched problem: {problem_id}\n"
        f"\n"
        f"  {msg}\n"
    )
    if server_msg:
        click.echo(f"  Server: {server_msg}\n")
    click.echo("You can now start working on the solution!")
    if capture_active:
        click.echo("\n" + _launch_instruction(workspace / problem_id))


@swe.command("submit")
@click.argument("problem_id")
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option(
    "--message", "-m", default=None,
    help="Commit message. Auto-generated if not provided.",
)
@click.option(
    "--force", is_flag=True, default=False,
    help="Force push (--force-with-lease). Use with caution.",
)
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Workspace directory. Overrides configured/cached value.",
)
@click.option(
    "--upload-logs/--no-upload-logs", "upload_logs", default=None,
    help="Override consent for uploading de-identified AI session logs.",
)
@click.option(
    "--logs-remote", default=None,
    help="Git URL to push AI session logs to (defaults to this problem's repo).",
)
def swe_submit(problem_id: str, user_id: str | None, message: str | None,
               force: bool, workspace_dir: str | None,
               upload_logs: bool | None, logs_remote: str | None):
    """Submit your SWE-bench solution by committing and pushing changes.

    Stages all changes, commits them, pushes to the remote, and notifies
    the AI Coding Gym server that your submission is ready for evaluation.

    \b
    PREREQUISITE:
      You must run 'aicodinggym swe fetch PROBLEM_ID' first.
      The fetch command sets up the repository and caches credentials
      needed for submission.

    \b
    ARGUMENTS:
      PROBLEM_ID  The problem identifier you fetched earlier.

    \b
    WHAT IT DOES:
      1. Stages all changed files (git add)
      2. Commits with your message (or auto-generated one)
      3. Pushes to the remote branch
      4. Notifies the backend for evaluation

    \b
    EXAMPLE:
      aicodinggym swe submit django__django-10097
      aicodinggym swe submit django__django-10097 -m "Fix auth validation bug"
      aicodinggym swe submit django__django-10097 --force
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)

    credentials = load_credentials()
    if problem_id not in credentials:
        _error(
            f"No credentials found for '{problem_id}'.\n\n"
            f"You must fetch the problem first:\n"
            f"  aicodinggym swe fetch {problem_id}\n\n"
            f"This clones the repository and saves the credentials needed for submission."
        )

    creds = credentials[problem_id]

    if creds.get("user_id") and creds["user_id"] != uid:
        _error(
            f"User ID mismatch. Problem was fetched by '{creds['user_id']}', not '{uid}'.\n"
            f"Either use --user-id {creds['user_id']} or re-fetch the problem."
        )

    workspace = _resolve_workspace(config, workspace_dir or creds.get("workspace_dir"))
    problem_dir = workspace / problem_id

    if not problem_dir.exists():
        _error(
            f"Problem directory not found at: {problem_dir}\n\n"
            f"You must fetch the problem first:\n"
            f"  aicodinggym swe fetch {problem_id}\n\n"
            f"Or specify the correct workspace with --workspace-dir."
        )

    key_path = _resolve_key_path(config, creds)
    branch = creds["branch"]
    commit_msg = message or f"Solution submission for {problem_id} at {datetime.now().isoformat()}"

    click.echo(f"Submitting solution for '{problem_id}'...")
    success, msg, commit_hash = add_commit_push(str(problem_dir), branch, key_path, commit_msg, force)

    if not success:
        _error(msg)

    # Notify backend
    try:
        submit_notification(
            problem_id=problem_id,
            user_id=uid,
            commit_hash=commit_hash,
            branch=branch,
            commit_message=commit_msg,
            timestamp=datetime.now().isoformat(),
        )
    except APIError as e:
        _warn(f"Changes pushed, but failed to notify backend: {e}")

    # Resolve logging (incl. any consent prompt) before the summary so the
    # prompt never interrupts the success banner. The solution commit above
    # already triggered Entire's checkpoint, so no flush is needed.
    log_status = _logging_status(lambda: _maybe_upload_logs(
        problem_dir, benchmark="swe", problem_id=problem_id, user_id=uid,
        key_path=key_path, config=config, creds=creds, upload_flag=upload_logs,
        logs_remote_override=logs_remote,
    ))

    summary = [
        f"\nSuccessfully submitted solution for {problem_id}\n",
        f"  Commit:  {commit_hash[:8]}",
        f"  Branch:  {branch}",
        f"  Status:  Pushed and backend notified",
    ]
    if log_status:
        summary.append(log_status)
    summary.append("")
    summary.append(f"View results at: {_hyperlink(f'https://aicodinggym.com/challenges/swe/{problem_id}')}")
    click.echo("\n".join(summary))


@swe.command("reset")
@click.argument("problem_id")
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Workspace directory. Overrides configured/cached value.",
)
def swe_reset(problem_id: str, user_id: str | None, workspace_dir: str | None):
    """Reset a SWE-bench problem to its original state.

    Discards all local changes and resets the repository back to the
    original setup commit. Use this to start over on a problem.

    \b
    WARNING: This is destructive. All your local changes will be lost.

    \b
    PREREQUISITE:
      You must run 'aicodinggym swe fetch PROBLEM_ID' first.

    \b
    ARGUMENTS:
      PROBLEM_ID  The problem identifier to reset.

    \b
    WHAT IT DOES:
      1. Finds the original 'Setup SWE-bench instance:' commit
      2. Runs git reset --hard to that commit
      3. Removes untracked files (git clean -fd)

    \b
    EXAMPLE:
      aicodinggym swe reset django__django-10097
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)

    credentials = load_credentials()
    if problem_id not in credentials:
        _error(
            f"No credentials found for '{problem_id}'.\n\n"
            f"You must fetch the problem first:\n"
            f"  aicodinggym swe fetch {problem_id}"
        )

    creds = credentials[problem_id]

    if creds.get("user_id") and creds["user_id"] != uid:
        _error(f"User ID mismatch. Problem was fetched by '{creds['user_id']}', not '{uid}'.")

    workspace = _resolve_workspace(config, workspace_dir or creds.get("workspace_dir"))
    problem_dir = workspace / problem_id

    if not problem_dir.exists():
        _error(f"Problem directory not found at: {problem_dir}")

    click.echo(f"Resetting '{problem_id}' to original state...")
    success, msg = reset_to_setup_commit(str(problem_dir))

    if not success:
        _error(msg)

    click.echo(f"\n{msg}")


@swe.command("test")
@click.argument("problem_id")
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Workspace directory. Overrides configured/cached value.",
)
@click.option(
    "-W", "workflow", default=None,
    help="Path to a specific workflow file relative to .github/workflows/. "
         "If omitted, act runs all workflows.",
)
@click.option(
    "--act-args", default=None,
    help="Additional arguments to pass to act (e.g. '--container-architecture linux/amd64').",
)
def swe_test(problem_id: str, user_id: str | None, workspace_dir: str | None,
             workflow: str | None, act_args: str | None):
    """Run the SWE-bench evaluation tests locally using nektos/act.

    Executes the GitHub Actions workflow from the problem repository on your
    local machine via 'act' (https://github.com/nektos/act), which requires
    Docker to be running.

    \b
    PREREQUISITES:
      1. Docker must be installed and running.
         Install: https://docs.docker.com/get-docker/
      2. 'act' must be installed.
         Install: https://github.com/nektos/act#installation
           macOS:   brew install act
           Linux:   curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash
      3. You must have fetched the problem first with 'aicodinggym swe fetch'.

    \b
    ARGUMENTS:
      PROBLEM_ID  The problem identifier you fetched earlier.

    \b
    WHAT IT DOES:
      1. Checks that Docker and act are installed
      2. Locates the .github/workflows/ directory in the problem repo
      3. Runs 'act' to execute the evaluation workflow locally
      4. Streams test output to your terminal

    \b
    EXAMPLE:
      aicodinggym swe test django-11400
      aicodinggym swe test django-11400 -W test_patch.yml
      aicodinggym swe test django-11400 --act-args '--container-architecture linux/amd64'
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)

    credentials = load_credentials()
    if problem_id not in credentials:
        _error(
            f"No credentials found for '{problem_id}'.\n\n"
            f"You must fetch the problem first:\n"
            f"  aicodinggym swe fetch {problem_id}"
        )

    creds = credentials[problem_id]

    if creds.get("user_id") and creds["user_id"] != uid:
        _error(f"User ID mismatch. Problem was fetched by '{creds['user_id']}', not '{uid}'.")

    workspace = _resolve_workspace(config, workspace_dir or creds.get("workspace_dir"))
    problem_dir = workspace / problem_id

    if not problem_dir.exists():
        _error(
            f"Problem directory not found at: {problem_dir}\n\n"
            f"You must fetch the problem first:\n"
            f"  aicodinggym swe fetch {problem_id}"
        )

    # ── Check dependencies ──────────────────────────────────────────────
    if not check_tool_installed("docker"):
        _error(
            "Docker is not installed or not on PATH.\n\n"
            "act requires Docker to run GitHub Actions workflows locally.\n\n"
            "Install Docker:\n"
            "  macOS / Windows: https://docs.docker.com/get-docker/\n"
            "  Ubuntu/Debian:   sudo apt-get install docker.io\n"
            "  Fedora:          sudo dnf install docker\n\n"
            "After installing, make sure the Docker daemon is running."
        )

    # Check Docker daemon is actually running
    try:
        docker_check = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10,
        )
        docker_running = docker_check.returncode == 0
    except subprocess.TimeoutExpired:
        docker_running = False
    if not docker_running:
        _error(
            "Docker is installed but the daemon is not running.\n\n"
            "Start Docker:\n"
            "  macOS:     open -a Docker\n"
            "  Windows:   Start Docker Desktop from the Start menu\n"
            "  Linux:     sudo systemctl start docker\n\n"
            "Wait a few seconds for it to start, then run this command again."
        )

    if not check_tool_installed("act"):
        _error(
            "'act' is not installed or not on PATH.\n\n"
            "act lets you run GitHub Actions workflows locally using Docker.\n"
            "https://github.com/nektos/act\n\n"
            "Install act:\n"
            "  macOS:     brew install act\n"
            "  Windows:   choco install act-cli  OR  winget install nektos.act\n"
            "  Linux:     curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash\n"
            "  Other:     https://github.com/nektos/act#installation\n\n"
            "After installing, run this command again."
        )

    # ── Ensure act config exists (avoids interactive prompt) ────────────
    _ensure_act_config()

    # ── Verify .github/workflows exists ─────────────────────────────────
    workflows_dir = problem_dir / ".github" / "workflows"
    if not workflows_dir.exists() or not any(workflows_dir.iterdir()):
        _error(
            f"No GitHub Actions workflows found at: {workflows_dir}\n\n"
            "The repository may not include evaluation workflows.\n"
            "Try re-fetching the problem:\n"
            f"  aicodinggym swe fetch {problem_id}"
        )

    # ── Build act command ───────────────────────────────────────────────
    act_cmd = ["act"]
    if workflow:
        act_cmd += ["-W", f".github/workflows/{workflow}"]

    # Auto-detect Apple Silicon: check if workflow needs x86_64 emulation
    if platform.machine() == "arm64" and (
        not act_args or "--container-architecture" not in act_args
    ):
        needs_amd64 = False
        for wf_file in workflows_dir.iterdir():
            content = wf_file.read_text()
            # Workflows with old Python, x86_64 conda packages, or
            # platform-specific binaries need amd64 emulation
            if re.search(r"python.*(3\.[56789]|2\.7)|libgcc|linux-64|linux_x86_64", content):
                needs_amd64 = True
                break
        if needs_amd64:
            click.echo(
                "Note: Detected x86_64-specific dependencies in workflow.\n"
                "      Running with --container-architecture linux/amd64 (slower on Apple Silicon).\n"
                "      Override with: --act-args '--container-architecture linux/arm64'\n"
            )
            act_cmd += ["--container-architecture", "linux/amd64"]

    if act_args:
        act_cmd += act_args.split()

    workflow_label = workflow or "all workflows"
    click.echo(f"Running local tests for '{problem_id}' ({workflow_label})...")
    click.echo(f"Command: {' '.join(act_cmd)}\n")

    # ── Run act (filter output, show detail only for test steps) ────────
    import time
    start_time = time.monotonic()
    output_lines: list[str] = []
    current_step = ""
    try:
        proc = subprocess.Popen(
            act_cmd, cwd=str(problem_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            output_lines.append(line)
            stripped = line.rstrip()

            # Step start: ⭐ Run <step name>
            step_start = re.search(r"⭐\s+Run\s+(.+)", stripped)
            if step_start:
                current_step = step_start.group(1).strip()
                click.echo(stripped)
                continue

            # Step result: ✅ Success / ❌ Failure
            if re.search(r"(✅\s+Success|❌\s+Failure)", stripped):
                click.echo(stripped)
                continue

            # Container lifecycle / job status / errors
            if re.search(r"(🚀|🏁|level=|Error response)", stripped):
                click.echo(stripped)
                continue

            # Detailed output (| ...) — only for specific steps
            if re.search(r"\|\s+", stripped):
                if "Apply Test Patch" in current_step:
                    click.echo(stripped)
                elif "Run Tests" in current_step:
                    # Only show test results, errors, tracebacks — skip DB setup noise
                    if re.search(
                        r"\.\.\.\s+(ok|FAIL|ERROR)"
                        r"|^.*\|\s+(FAIL|ERROR|OK|FAILED|Running \w+_TO_\w+:|Ran \d+)"
                        r"|^.*\|\s+(Traceback|File |AssertionError|ImportError|ModuleNotFoundError)"
                        r"|^.*\|\s+[-=]{10,}",
                        stripped,
                    ):
                        click.echo(stripped)

        proc.wait()
    except FileNotFoundError:
        _error("Failed to execute 'act'. Make sure it is installed and on your PATH.")
    except KeyboardInterrupt:
        proc.kill()
        click.echo("\nTest run interrupted.")
        sys.exit(130)

    # ── Parse and display summary ───────────────────────────────────────
    elapsed = time.monotonic() - start_time
    _print_test_summary(output_lines, problem_id, proc.returncode, elapsed)

    if proc.returncode != 0:
        sys.exit(proc.returncode)


# ── cr group ──────────────────────────────────────────────────────────────────


@main.group()
def cr():
    """Code Review challenges - submit reviews for code diffs.

    \b
    PREREQUISITE:
      Run 'aicodinggym configure --user-id YOUR_USER_ID' before using these commands.

    \b
    WORKFLOW:
      1. aicodinggym cr fetch CR_PROBLEM_ID             # Download diff + create review.md
      2. (edit review.md with your findings)
      3. aicodinggym cr submit CR_PROBLEM_ID -f review.md   # Submit your review
    """
    pass


@cr.command("fetch")
@click.argument("problem_id")
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option("--workspace-dir", default=None, type=click.Path(),
              help="Directory to clone into. Overrides configured workspace.")
def cr_fetch(problem_id: str, user_id: str | None, workspace_dir: str | None):
    """Fetch a Code Review problem: downloads the PR diff and creates a review template.

    Clones the repository, generates diff.patch from base→head, and creates
    review.md as a template to fill in your review.

    \b
    ARGUMENTS:
      PROBLEM_ID  The code review problem identifier (e.g., 'keycloak-0008').

    \b
    EXAMPLE:
      aicodinggym cr fetch keycloak-0008
      # Edit review.md in the problem directory, then:
      aicodinggym cr submit keycloak-0008 -f review.md
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)
    workspace = _resolve_workspace(config, workspace_dir)

    try:
        click.echo(f"Fetching problem '{problem_id}' from server...")
        data = api_fetch_pr(uid, problem_id)
    except APIError as e:
        _error(str(e))

    base_branch = data.get("base_branch")
    head_branch = data.get("head_branch")
    repo_url = data.get("repo_url")

    if not (repo_url and repo_url.strip()) or not (base_branch and base_branch.strip()) or not (head_branch and head_branch.strip()):
        _error("Server did not return required fields (repo_url, base_branch, head_branch).")

    # Save credentials for later submit
    credentials = load_credentials()
    credentials[problem_id] = {
        "repo_url": repo_url,
        "base_branch": base_branch,
        "head_branch": head_branch,
        "user_id": uid,
        "workspace_dir": str(workspace),
        "benchmark": "cr",
    }
    save_credentials(credentials)

    workspace.mkdir(parents=True, exist_ok=True)

    click.echo(f"Cloning into {workspace / problem_id}...")
    success, msg = clone_repo_cr(repo_url, base_branch, head_branch,
                                  problem_id, str(workspace))
    if not success:
        _error(msg)

    _install_gym_environment(workspace / problem_id)
    capture_active = _setup_logging(workspace / problem_id, user_id=uid)

    problem_dir = workspace / problem_id

    # Generate diff.patch
    diff_result = run_git_command(
        ["git", "diff", f"{base_branch}..{head_branch}"], str(problem_dir)
    )
    diff_path = problem_dir / "diff.patch"
    diff_path.write_text(diff_result.stdout)

    # Create review.md template if it doesn't exist yet
    review_path = problem_dir / "review.md"
    if not review_path.exists():
        review_path.write_text(
            f"# Code Review: {problem_id}\n\n"
            "## Summary\n\n"
            "<!-- Brief summary of what this PR does -->\n\n"
            "## Issues Found\n\n"
            "<!-- List bugs, logic errors, security issues, etc. -->\n\n"
            "## Suggestions\n\n"
            "<!-- Optional improvements, style notes, etc. -->\n\n"
            "## Verdict\n\n"
            "<!-- Approve / Request Changes / Comment -->\n"
        )

    cat_cmd = "type" if sys.platform == "win32" else "cat"
    click.echo(
        f"\nSuccessfully fetched: {problem_id}\n"
        f"\n"
        f"  Diff saved to:     {diff_path}\n"
        f"  Review template:   {review_path}\n"
        f"\n"
        f"Next steps:\n"
        f"  1. Review the diff:  {cat_cmd} {diff_path}\n"
        f"  2. Write your review in {review_path}\n"
        f"  3. Submit:  aicodinggym cr submit {problem_id} -f review.md\n"
    )
    if capture_active:
        click.echo("\n" + _launch_instruction(problem_dir))


@cr.command("submit")
@click.argument("problem_id")
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option(
    "-f", "--file", "review_file", type=click.Path(exists=True),
    help="Path to a file containing your review.",
)
@click.option(
    "-m", "--message", "review_text",
    help="Inline review text.",
)
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Workspace directory. Overrides configured/cached value.",
)
@click.option(
    "--upload-logs/--no-upload-logs", "upload_logs", default=None,
    help="Override consent for uploading de-identified AI session logs.",
)
@click.option(
    "--logs-remote", default=None,
    help="Git URL to push AI session logs to (defaults to your submission repo).",
)
def cr_submit(problem_id: str, user_id: str | None, review_file: str | None,
              review_text: str | None, workspace_dir: str | None,
              upload_logs: bool | None, logs_remote: str | None):
    """Submit a code review for a Code Review challenge.

    Reads your review from a file (-f), inline text (-m), or piped stdin,
    and submits it to the AI Coding Gym server.

    \b
    ARGUMENTS:
      PROBLEM_ID  The code review problem identifier (e.g., 'sentry-0001').

    \b
    EXAMPLE:
      aicodinggym cr submit sentry-0001 -f review.md
      aicodinggym cr submit sentry-0001 -m "Found a null pointer bug on line 42"
      echo "My review" | aicodinggym cr submit sentry-0001
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)

    # Collect review text (priority: -f > -m > stdin)
    review = None
    if review_file:
        review = Path(review_file).read_text()
    elif review_text:
        review = review_text
    elif not sys.stdin.isatty():
        review = sys.stdin.read()

    if not review or not review.strip():
        _error(
            "No review text provided.\n\n"
            "Provide your review using one of:\n"
            "  -f <file>       Read review from a file\n"
            "  -m \"text\"       Inline review text\n"
            "  echo ... | ...  Pipe from stdin\n\n"
            "Example:\n"
            f"  aicodinggym cr submit {problem_id} -f review.md"
        )

    try:
        result = cr_submit_review(uid, problem_id, review.strip())
    except APIError as e:
        _error(str(e))

    # CR clones a read-only PR, so logs upload to the user's own submission repo.
    # The CR flow makes no commit, so flush=True materialises the checkpoint.
    # Resolve before the banner so any consent prompt comes first.
    creds = load_credentials().get(problem_id)
    workspace = _resolve_workspace(config, workspace_dir or (creds or {}).get("workspace_dir"))
    problem_dir = workspace / problem_id
    log_status = ""
    if problem_dir.exists():
        log_status = _logging_status(lambda: _maybe_upload_logs(
            problem_dir, benchmark="cr", problem_id=problem_id, user_id=uid,
            key_path=_safe_key_path(config, creds), config=config, creds=creds,
            upload_flag=upload_logs, logs_remote_override=logs_remote, flush=True,
        ))

    summary = [
        f"\nSuccessfully submitted code review for {problem_id}\n",
        f"  Status:  {result.get('status', 'COMPLETED')}",
    ]
    if log_status:
        summary.append(log_status)
    summary.append("")
    summary.append(f"View results at: {_hyperlink(f'https://aicodinggym.com/challenges/cr/{problem_id}')}")
    click.echo("\n".join(summary))


# ── mle group ────────────────────────────────────────────────────────────────


@main.group()
def mle():
    """MLE-bench ML competitions - download data and submit predictions.

    \b
    PREREQUISITE:
      Run 'aicodinggym configure --user-id YOUR_USER_ID' before using these commands.

    \b
    WORKFLOW:
      1. aicodinggym mle download COMPETITION_ID                        # Download dataset
      2. (train your model and generate predictions)                    # Work on your solution
      3. aicodinggym mle submit COMPETITION_ID -F submission.csv     # Submit predictions

    \b
    COMPETITION IDS:
      Examples: spaceship-titanic, house-prices, digit-recognizer
      Browse competitions at https://aicodinggym.com
    """
    pass


@mle.command("download")
@click.argument("competition_id")
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Workspace directory. Defaults to configured workspace.",
)
def mle_download(competition_id: str, user_id: str | None, workspace_dir: str | None):
    """Download dataset files for an MLE-bench competition.

    \b
    EXAMPLE:
      aicodinggym mle download spaceship-titanic
      aicodinggym mle download spaceship-titanic --workspace-dir ~/workspace
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)

    workspace = _resolve_workspace(config, workspace_dir)
    dest_dir = workspace / competition_id / "data"
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{competition_id}.zip"
    dest_path = dest_dir / filename

    try:
        click.echo(f"Downloading dataset for '{competition_id}'...")
        total, chunks = mlebench_download_open(competition_id)
        with open(dest_path, "wb") as f:
            if total:
                with click.progressbar(length=total, label="  Downloading",
                                       show_percent=True) as bar:
                    for chunk in chunks:
                        f.write(chunk)
                        bar.update(len(chunk))
            else:
                # Server didn't advertise a size — show a running byte counter.
                downloaded = 0
                for chunk in chunks:
                    f.write(chunk)
                    downloaded += len(chunk)
                    click.echo(f"\r  {downloaded / 1_048_576:.1f} MB downloaded", nl=False)
                click.echo()
    except APIError as e:
        _error(str(e))

    _install_gym_environment(workspace / competition_id)
    # MLE workspaces aren't git repos: init one so the solution code can be
    # pushed on submit and Entire can attach for session capture.
    entire_logging.ensure_git_repo(workspace / competition_id)
    capture_active = _setup_logging(workspace / competition_id, user_id=uid)

    click.echo(
        f"\nDataset downloaded to: {dest_path}\n"
        f"\nNext step: train your model and submit predictions with:\n"
        f"  aicodinggym mle submit {competition_id} -F your_predictions.csv"
    )
    if capture_active:
        click.echo("\n" + _launch_instruction(workspace / competition_id))


@mle.command("submit")
@click.argument("competition_id")
@click.option(
    "-F", "csv_path", required=True, type=click.Path(exists=True),
    help="Path to your prediction CSV file (required).",
)
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option(
    "--message", "-m", default=None,
    help="Description of your submission (optional).",
)
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Workspace directory. Overrides configured value.",
)
@click.option(
    "--upload-logs/--no-upload-logs", "upload_logs", default=None,
    help="Override consent for uploading de-identified AI session logs.",
)
@click.option(
    "--logs-remote", default=None,
    help="Git URL to push AI session logs to (defaults to your submission repo).",
)
def mle_submit(competition_id: str, csv_path: str, user_id: str | None,
               message: str | None, workspace_dir: str | None,
               upload_logs: bool | None, logs_remote: str | None):
    """Submit a prediction CSV for an MLE-bench competition.

    Uploads your prediction CSV directly to the AI Coding Gym server
    for scoring.

    \b
    PREREQUISITE:
      You must run 'aicodinggym configure --user-id YOUR_USER_ID' first.

    \b
    ARGUMENTS:
      COMPETITION_ID  The competition identifier (e.g., 'spaceship-titanic').

    \b
    OPTIONS:
      -F FILE  Path to your prediction CSV file. This is REQUIRED.
               The file must exist and be a valid CSV matching the
               competition's expected format (see sample_submission.csv).

    \b
    WHAT IT DOES:
      1. Validates that the CSV file exists
      2. Uploads the CSV to the AI Coding Gym server
      3. Server scores your predictions and returns results

    \b
    EXAMPLE:
      aicodinggym mle submit spaceship-titanic -F predictions.csv
      aicodinggym mle submit spaceship-titanic -F ./output/pred.csv -m "XGBoost v2"
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)

    csv_src = Path(csv_path).resolve()

    click.echo(f"Uploading {csv_src.name} for '{competition_id}'...")

    try:
        result = mlebench_submit_csv(uid, competition_id, str(csv_src))
    except APIError as e:
        _error(str(e))

    score_msg = result.get("message", "Submission received for scoring.")
    score = result.get("score")

    # Push the user's solution code + (with consent) the AI session logs to the
    # user's own repo, each on a unique per-submission branch. Resolve before
    # the banner so any consent prompt comes first.
    workspace = _resolve_workspace(config, workspace_dir)
    competition_dir = workspace / competition_id
    artifacts_status = ""
    if competition_dir.exists():
        artifacts_status = _logging_status(lambda: _maybe_submit_mle_artifacts(
            competition_dir, competition_id=competition_id, user_id=uid, config=config,
            key_path=_safe_key_path(config), upload_flag=upload_logs,
            logs_remote_override=logs_remote,
        ))

    summary = [
        f"\nSuccessfully submitted prediction for {competition_id}\n",
        f"  CSV:     {csv_src.name}",
        f"  Status:  {score_msg}",
    ]
    if score is not None:
        summary.append(f"  Score:   {score}")
    if artifacts_status:
        summary.append(artifacts_status)
    summary.append("")
    summary.append(f"View results at: {_hyperlink(f'https://aicodinggym.com/challenges/mle/{competition_id}')}")
    click.echo("\n".join(summary))


@mle.command("restore")
@click.argument("competition_id")
@click.option("--user-id", default=None, help="Override configured user ID.")
@click.option(
    "--workspace-dir", default=None, type=click.Path(),
    help="Workspace directory. Overrides configured value.",
)
@click.option(
    "--branch", default=None,
    help="Branch to restore (defaults to the competition name). Pass a full "
         "'<competition>/<stamp>' branch to restore an older submission.",
)
@click.option(
    "--remote", default=None,
    help="Git URL to restore from (defaults to your submission repo).",
)
@click.option(
    "--force", is_flag=True, default=False,
    help="Overwrite uncommitted local changes in the workspace.",
)
def mle_restore(competition_id: str, user_id: str | None, workspace_dir: str | None,
                branch: str | None, remote: str | None, force: bool):
    """Restore a competition workspace from your submission repo.

    Pulls the solution code you pushed on a previous 'mle submit' back into your
    workspace — e.g. to pick up on another machine or recover after a reset. Your
    downloaded dataset and other gitignored files are left untouched.

    \b
    EXAMPLE:
      aicodinggym mle restore spaceship-titanic
      aicodinggym mle restore spaceship-titanic --branch spaceship-titanic/20260608T213116Z-5252df01
    """
    config = load_config()
    uid = _resolve_user_id(config, user_id)

    remote_url = _resolve_logs_remote("mle", None, config, remote)
    if not remote_url:
        _error(
            "No submission repository is configured to restore from.\n"
            "Run 'aicodinggym configure --user-id YOUR_USER_ID' first, or pass --remote URL."
        )

    workspace = _resolve_workspace(config, workspace_dir)
    target = workspace / competition_id
    branch_name = branch or competition_id

    click.echo(f"Restoring '{branch_name}' from your submission repo into {target}...")
    ok, msg = restore_branch(
        remote_url, branch_name, str(target),
        key_path=_safe_key_path(config), force=force,
    )
    if not ok:
        _error(msg)

    # Re-arm local AI-session capture for continued work in the restored repo.
    _setup_logging(target, user_id=uid)

    click.echo(
        f"\n{msg}\n"
        f"\nNext step: continue working, then submit with:\n"
        f"  aicodinggym mle submit {competition_id} -F your_predictions.csv"
    )
