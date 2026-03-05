"""AI Coding Gym CLI - main entry point.

A command-line tool for the AI Coding Gym platform (https://aicodinggym.com).
Supports SWE-bench and MLE-bench challenges.

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
"""

import sys
from datetime import datetime
from pathlib import Path

import click

from . import __version__
from .api import (
    APIError,
    configure as api_configure,
    fetch_problem as api_fetch_problem,
    mlebench_download_file,
    mlebench_download_info,
    mlebench_submit_csv,
    submit_notification,
)
from .config import (
    load_config,
    load_credentials,
    save_config,
    save_credentials,
)
from .git_ops import (
    add_commit_push,
    clone_repo,
    generate_ssh_key_pair,
    reset_to_setup_commit,
)


def _error(msg: str) -> None:
    """Print an error message to stderr and exit."""
    click.echo(f"Error: {msg}", err=True)
    sys.exit(1)


def _warn(msg: str) -> None:
    """Print a warning message to stderr."""
    click.echo(f"Warning: {msg}", err=True)


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
        "  aicodinggym mle submit spaceship-titanic -F predictions.csv\n\n"
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
def configure(user_id: str, workspace_dir: str | None):
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
        try:
            data = api_configure(user_id, public_key)
            repo_name = data.get("repo_name")
            if not repo_name:
                _error("Server did not return a repository name. Please try again or contact support.")
        except APIError as e:
            if "409" in str(e):
                click.echo("Key already registered, reusing existing configuration.")
                existing = load_config()
                repo_name = existing.get("repo_name", f"submission-{user_id}")
            else:
                raise

        resolved_workspace = str(Path(workspace_dir).resolve()) if workspace_dir else str(Path.cwd().resolve())

        config = {
            "user_id": user_id,
            "repo_name": repo_name,
            "private_key_path": str(private_key_path),
            "workspace_dir": resolved_workspace,
        }
        save_config(config)

        click.echo(
            f"\nConfiguration saved successfully!\n"
            f"\n"
            f"  User ID:     {user_id}\n"
            f"  Repository:  {repo_name}\n"
            f"  Workspace:   {resolved_workspace}\n"
            f"  SSH Key:     {private_key_path}\n"
            f"  Config:      ~/.aicodinggym/config.json\n"
            f"\n"
            f"You can now use 'aicodinggym swe' and 'aicodinggym mle' commands."
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

    click.echo(
        f"\nSuccessfully fetched problem: {problem_id}\n"
        f"\n"
        f"  {msg}\n"
    )
    if server_msg:
        click.echo(f"  Server: {server_msg}\n")
    click.echo("You can now start working on the solution!")


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
def swe_submit(problem_id: str, user_id: str | None, message: str | None,
               force: bool, workspace_dir: str | None):
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

    click.echo(
        f"\nSuccessfully submitted solution for {problem_id}\n"
        f"\n"
        f"  Commit:  {commit_hash[:8]}\n"
        f"  Branch:  {branch}\n"
        f"  Status:  Pushed and backend notified\n"
        f"\n"
        f"Your solution has been submitted for evaluation!"
    )


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
        mlebench_download_info(uid, competition_id, str(dest_path))
    except APIError as e:
        _error(str(e))

    click.echo(
        f"\nDataset downloaded to: {dest_path}\n"
        f"\nNext step: train your model and submit predictions with:\n"
        f"  aicodinggym mle submit {competition_id} -F your_predictions.csv"
    )


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
def mle_submit(competition_id: str, csv_path: str, user_id: str | None,
               message: str | None):
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

    click.echo(
        f"\nSuccessfully submitted prediction for {competition_id}\n"
        f"\n"
        f"  CSV:     {csv_src.name}\n"
        f"  Status:  {score_msg}\n"
    )
    if score is not None:
        click.echo(f"  Score:   {score}\n")
    click.echo("Your prediction has been submitted for scoring!")
