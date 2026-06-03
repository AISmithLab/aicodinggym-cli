"""Optional AI-workflow logging via the Entire CLI (https://entire.io).

Entire hooks into the local git workflow to capture AI agent sessions
(prompts, responses, tool calls, files touched) and stores them on a separate
``entire/checkpoints/v1`` git branch — it never adds commits to your working
branch. AI Coding Gym uses this to study *how* solutions are produced.

This module is a thin, best-effort wrapper around the ``entire`` binary:

* :func:`setup` is called at fetch/download time to install Entire's hooks so
  capture happens locally as the user works. Nothing leaves the machine here.
* :func:`upload` is called at submit time *after the user consents*. It pushes
  the captured ``entire/checkpoints/v1`` branch to a writable git remote, under
  a per-problem branch name so each upload is identifiable, with an
  ``aicodinggym-meta.json`` metadata file injected at the tip.

Every function degrades gracefully: if the ``entire`` binary is missing or a
command fails, AI Coding Gym's core fetch/submit flow is never blocked.

Privacy: capture is local-only until the user opts in. Uploaded data is used
solely for research and is de-identified/anonymized before use. Entire also
redacts detected secrets when writing to the checkpoints branch (best-effort).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import git_ops


# Branch Entire writes captured sessions to (fixed by Entire).
CHECKPOINT_BRANCH = "entire/checkpoints/v1"

# Metadata file injected at the tip of each uploaded log branch.
METADATA_FILENAME = "aicodinggym-meta.json"

# Primary agent to capture (this CLI is commonly driven by Claude Code), plus
# extra agents we enable when their binary is detected on PATH. Keys are the
# names Entire expects for `entire agent add`; values are the PATH binary.
PRIMARY_AGENT = "claude-code"
OPTIONAL_AGENTS = {
    "codex": "codex",
    "gemini": "gemini",
    "cursor": "cursor-agent",
    "opencode": "opencode",
}

# Identity used for the synthetic flush/metadata commits so we never depend on
# (or alter) the user's configured git identity.
_LOG_IDENT = {
    "GIT_AUTHOR_NAME": "AI Coding Gym",
    "GIT_AUTHOR_EMAIL": "logs@aicodinggym.com",
    "GIT_COMMITTER_NAME": "AI Coding Gym",
    "GIT_COMMITTER_EMAIL": "logs@aicodinggym.com",
}

_FLUSH_MESSAGE = "AI Coding Gym: capture AI session checkpoint"

if sys.platform == "win32":
    INSTALL_COMMAND = (
        "scoop bucket add entire https://github.com/entireio/scoop-bucket.git; "
        "scoop install entire/cli"
    )
else:
    INSTALL_COMMAND = "curl -fsSL https://entire.io/install.sh | bash"


# ── binary discovery / install ───────────────────────────────────────────────


def is_available() -> bool:
    """True if the ``entire`` binary is on PATH."""
    return shutil.which("entire") is not None


def version() -> str | None:
    """Return Entire's reported version, or None if unavailable."""
    if not is_available():
        return None
    res = _entire(["version"], cwd=Path.cwd())
    if res.returncode == 0:
        return res.stdout.strip() or None
    return None


def install() -> tuple[bool, str]:
    """Run Entire's official installer. Returns (installed_now, message).

    Best-effort: on failure the caller should fall back to printing
    :data:`INSTALL_COMMAND` for the user to run manually.
    """
    try:
        if sys.platform == "win32":
            # Scoop lives behind PowerShell; auto-driving it reliably is brittle,
            # so we defer to the documented manual command.
            return False, "automatic install is not supported on Windows"
        res = subprocess.run(
            ["bash", "-c", INSTALL_COMMAND],
            capture_output=True, text=True, timeout=300,
        )
        if res.returncode != 0:
            return False, (res.stderr or res.stdout or "installer exited non-zero").strip()
        # shutil.which caches nothing, but the new binary may have landed in a
        # dir not yet on this process's PATH (e.g. ~/.local/bin).
        if is_available():
            return True, "installed"
        return True, "installed (you may need to open a new shell for it to appear on PATH)"
    except FileNotFoundError:
        return False, "bash not found"
    except subprocess.TimeoutExpired:
        return False, "installer timed out"
    except Exception as e:  # noqa: BLE001 - never let install crash configure
        return False, str(e)


# ── repo-level helpers ───────────────────────────────────────────────────────


def is_enabled(repo_dir: Path) -> bool:
    """True if Entire has been set up in this repo (``.entire/`` present)."""
    return (Path(repo_dir) / ".entire").is_dir()


def has_sessions(repo_dir: Path) -> bool:
    """True if a captured-session checkpoint branch exists locally."""
    res = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{CHECKPOINT_BRANCH}"],
               cwd=repo_dir)
    return res.returncode == 0 and bool(res.stdout.strip())


def setup(repo_dir: Path, *, init_git: bool = False) -> tuple[bool, str]:
    """Install Entire hooks so this repo captures AI sessions locally.

    Captures only — sessions are not pushed here (``--skip-push-sessions``);
    upload happens later, with consent, in :func:`upload`.

    Set ``init_git=True`` for non-git workspaces (e.g. MLE competition dirs):
    a lightweight repo is initialised first so Entire has something to attach
    to. Returns (ok, message); never raises.
    """
    if not is_available():
        return False, "entire not installed"

    repo_dir = Path(repo_dir)
    try:
        if init_git and not (repo_dir / ".git").exists():
            ok, msg = ensure_git_repo(repo_dir)
            if not ok:
                return False, msg
        if not (repo_dir / ".git").exists():
            return False, "not a git repository"

        enable = _entire(
            ["enable", "--agent", PRIMARY_AGENT, "--skip-push-sessions", "--telemetry=false"],
            cwd=repo_dir,
        )
        if enable.returncode != 0 and not is_enabled(repo_dir):
            return False, (enable.stderr or enable.stdout or "entire enable failed").strip()

        enabled_agents = [PRIMARY_AGENT]
        for agent_name, binary in OPTIONAL_AGENTS.items():
            if shutil.which(binary):
                add = _entire(["agent", "add", agent_name], cwd=repo_dir)
                if add.returncode == 0:
                    enabled_agents.append(agent_name)

        return True, "capturing AI sessions for: " + ", ".join(enabled_agents)
    except Exception as e:  # noqa: BLE001 - logging must never break fetch
        return False, str(e)


def flush(repo_dir: Path) -> None:
    """Materialise a checkpoint from the active session, best-effort.

    Entire writes checkpoints on commit. Flows that already commit (SWE submit)
    don't need this; flows that don't (CR submit) call this to trigger Entire's
    post-commit hook via an empty commit. The empty commit stays local — only
    the resulting ``entire/checkpoints/v1`` branch is ever pushed.
    """
    if not is_available() or not is_enabled(repo_dir):
        return
    try:
        _git(
            ["-c", "user.name=AI Coding Gym", "-c", "user.email=logs@aicodinggym.com",
             "commit", "--allow-empty", "-m", _FLUSH_MESSAGE],
            cwd=repo_dir, env_extra=_LOG_IDENT,
        )
    except Exception:  # noqa: BLE001
        pass


def commit_workspace(repo_dir: Path, message: str) -> bool:
    """Stage and commit the whole working tree (gitignored files excluded).

    Used by MLE submit to record the user's solution code; the commit also
    triggers Entire's post-commit hook (so it doubles as a checkpoint flush
    when Entire is enabled). Returns True if a commit was made.
    """
    if not (Path(repo_dir) / ".git").exists():
        return False
    try:
        _git(["add", "-A"], cwd=repo_dir)
        res = _git(
            ["-c", "user.name=AI Coding Gym", "-c", "user.email=logs@aicodinggym.com",
             "commit", "--allow-empty", "-m", message],
            cwd=repo_dir, env_extra=_LOG_IDENT,
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def push_branch(repo_dir: Path, *, remote_url: str, dest_branch: str,
                key_path: Path | None = None) -> tuple[bool, str]:
    """Push the current HEAD to a fresh ``dest_branch`` on ``remote_url``.

    Used for MLE to push the user's solution code. The caller passes a unique,
    per-submission branch name (see :func:`new_stamp`), so we do NOT force-push:
    each submission lands on its own branch and previous ones are preserved.
    Returns (ok, branch_or_error). Never raises.
    """
    try:
        safe = _safe_ref(dest_branch)
        refspec = f"HEAD:refs/heads/{safe}"
        res = git_ops.run_git_command(
            ["git", "push", remote_url, refspec], str(repo_dir), key_path,
        )
        if res.returncode != 0:
            return False, (res.stderr or "git push failed").strip()
        return True, safe
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def new_stamp() -> str:
    """A unique, sortable per-submission id: ``<UTC-timestamp>-<random>``.

    Used to give every upload its own branch so re-submissions — and submissions
    of the same problem from different directories/machines — never overwrite
    each other's logs.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def logs_branch(benchmark: str, problem_id: str, suffix: str | None = None) -> str:
    """Remote branch identifying which problem (and submission) a log belongs to.

    ``aicodinggym-logs/<benchmark>/<problem_id>`` — and when ``suffix`` is given
    (the per-submission stamp), ``.../<problem_id>/<suffix>`` so each upload is a
    distinct, non-overwriting branch.
    """
    parts = ["aicodinggym-logs", benchmark, _safe_ref(problem_id)]
    if suffix:
        parts.append(_safe_ref(suffix))
    return "/".join(parts)


def upload(repo_dir: Path, *, remote_url: str, benchmark: str, problem_id: str,
           user_id: str, key_path: Path | None = None, tool: str | None = None,
           cli_version: str | None = None,
           submission_stamp: str | None = None) -> tuple[bool, str]:
    """Push the captured session branch to ``remote_url`` for research.

    Pushes ``entire/checkpoints/v1`` to a unique per-submission branch
    (:func:`logs_branch` with a stamp), after injecting an
    ``aicodinggym-meta.json`` metadata file at the tip so each upload is
    self-describing. The unique branch means previous logs are never
    overwritten. Returns (ok, branch_or_error). Never raises.
    """
    repo_dir = Path(repo_dir)
    try:
        tip = _git(["rev-parse", "--verify", f"refs/heads/{CHECKPOINT_BRANCH}"], cwd=repo_dir)
        if tip.returncode != 0 or not tip.stdout.strip():
            return False, "no captured sessions to upload"
        parent = tip.stdout.strip()

        stamp = submission_stamp or new_stamp()
        meta = {
            "problem_id": problem_id,
            "benchmark": benchmark,
            "user_id": user_id,
            "tool": tool,
            "cli_version": cli_version,
            "submission_id": stamp,
            "captured_by": "aicodinggym-cli",
            "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # Inject metadata as an extra commit on top of the checkpoint tip,
        # without disturbing the working tree or Entire's branch. Falls back to
        # the raw tip if plumbing fails — the branch name still identifies it.
        push_sha = _commit_with_metadata(repo_dir, parent, meta) or parent

        dest = logs_branch(benchmark, problem_id, stamp)
        refspec = f"{push_sha}:refs/heads/{dest}"
        # No force: a fresh per-submission branch, so nothing is overwritten.
        push = git_ops.run_git_command(
            ["git", "push", remote_url, refspec], str(repo_dir), key_path,
        )
        if push.returncode != 0:
            return False, (push.stderr or "git push failed").strip()
        return True, dest
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ── internals ────────────────────────────────────────────────────────────────


def _safe_ref(name: str) -> str:
    """Sanitise a string into a valid git ref path component."""
    safe = re.sub(r"[ \t~^:?*\[\]\\\x00-\x1f\x7f]", "_", name)
    safe = safe.replace("..", "_").strip("/")
    return safe or "_"


def _entire(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run the ``entire`` binary, capturing output. Non-interactive."""
    env = os.environ.copy()
    env.setdefault("ACCESSIBLE", "1")  # avoid interactive TUI elements
    return subprocess.run(
        ["entire", *args], cwd=str(cwd), capture_output=True, text=True, env=env,
    )


def _git(args: list[str], cwd: Path, *, env_extra: dict | None = None,
         input_text: str | None = None) -> subprocess.CompletedProcess:
    """Run a local git command (no network) with optional extra env."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env=env, input=input_text,
    )


def ensure_git_repo(repo_dir: Path) -> tuple[bool, str]:
    """Initialise a minimal git repo so Entire can attach and MLE code can be
    pushed (MLE workspaces aren't git repos by default). No-op if already a repo."""
    if (Path(repo_dir) / ".git").exists():
        return True, "already a git repo"
    init = _git(["init", "-q"], cwd=repo_dir)
    if init.returncode != 0:
        return False, (init.stderr or "git init failed").strip()

    # Keep heavy/derived ML artifacts out of the repo so the code branch we push
    # on submit stays small. The dataset and common model/checkpoint/cache files
    # are excluded; the user's notebooks/scripts and submission CSV are kept.
    gitignore = repo_dir / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    wanted = [
        # dataset & archives
        "data/", "*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.7z",
        # python / tooling caches
        "__pycache__/", "*.py[cod]", ".ipynb_checkpoints/",
        ".venv/", "venv/", "env/", ".env",
        ".entire/",
        # model weights / serialized artifacts
        "*.pkl", "*.pickle", "*.joblib", "*.npy", "*.npz",
        "*.h5", "*.hdf5", "*.pt", "*.pth", "*.ckpt", "*.onnx", "*.pb", "*.bin", "*.safetensors",
        # experiment-tracking / output dirs
        "wandb/", "mlruns/", "lightning_logs/", "runs/",
        "checkpoints/", "outputs/", "artifacts/", "models/",
        # logs
        "*.log",
    ]
    missing = [w for w in wanted if w not in existing.splitlines()]
    if missing:
        block = ("" if existing.endswith("\n") or not existing else "\n") + \
                "\n# aicodinggym logging\n" + "\n".join(missing) + "\n"
        with open(gitignore, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(block)

    _git(["add", ".gitignore"], cwd=repo_dir)
    commit = _git(
        ["-c", "user.name=AI Coding Gym", "-c", "user.email=logs@aicodinggym.com",
         "commit", "--allow-empty", "-m", "AI Coding Gym: initialize logging workspace"],
        cwd=repo_dir, env_extra=_LOG_IDENT,
    )
    if commit.returncode != 0:
        return False, (commit.stderr or "initial commit failed").strip()
    return True, "initialized git repo"


def _commit_with_metadata(repo_dir: Path, parent_sha: str, meta: dict) -> str | None:
    """Return a new commit SHA = parent_sha + an aicodinggym-meta.json file.

    Uses git plumbing with a throwaway index so neither the working tree nor
    Entire's checkpoint branch is touched. Returns None on any failure.
    """
    try:
        blob = _git(["hash-object", "-w", "--stdin"], cwd=repo_dir,
                    input_text=json.dumps(meta, indent=2) + "\n")
        if blob.returncode != 0 or not blob.stdout.strip():
            return None
        blob_sha = blob.stdout.strip()

        index_path = Path(repo_dir) / ".git" / "aicodinggym_meta.index"
        index_env = {"GIT_INDEX_FILE": str(index_path)}
        try:
            if _git(["read-tree", parent_sha], cwd=repo_dir, env_extra=index_env).returncode != 0:
                return None
            added = _git(
                ["update-index", "--add", "--cacheinfo",
                 f"100644,{blob_sha},{METADATA_FILENAME}"],
                cwd=repo_dir, env_extra=index_env,
            )
            if added.returncode != 0:
                return None
            tree = _git(["write-tree"], cwd=repo_dir, env_extra=index_env)
            if tree.returncode != 0 or not tree.stdout.strip():
                return None
            tree_sha = tree.stdout.strip()
        finally:
            try:
                index_path.unlink()
            except OSError:
                pass

        message = f"AI Coding Gym session log: {meta.get('benchmark')}/{meta.get('problem_id')}"
        commit = _git(
            ["commit-tree", tree_sha, "-p", parent_sha, "-m", message],
            cwd=repo_dir, env_extra=_LOG_IDENT,
        )
        if commit.returncode != 0 or not commit.stdout.strip():
            return None
        return commit.stdout.strip()
    except Exception:  # noqa: BLE001
        return None
