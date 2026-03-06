"""Git and SSH key operations for AI Coding Gym CLI."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import ensure_config_dir


def generate_ssh_key_pair(user_id: str) -> tuple[Path, str]:
    """Generate an SSH key pair for the user.

    First checks ~/.mcp-keys/ for existing keys matching the user_id.
    If found, copies them to ~/.aicodinggym/ and reuses them.
    Otherwise generates new keys in ~/.aicodinggym/{user_id}_id_rsa.
    Returns (private_key_path, public_key_content).
    """
    key_dir = ensure_config_dir()
    key_path = key_dir / f"{user_id}_id_rsa"

    if not key_path.exists():
        # Check ~/.mcp-keys/ for existing keys matching user_id
        mcp_keys_dir = Path.home() / ".mcp-keys"
        mcp_private = mcp_keys_dir / f"{user_id}_id_rsa"
        mcp_public = mcp_keys_dir / f"{user_id}_id_rsa.pub"

        if mcp_private.exists() and mcp_public.exists():
            shutil.copy2(mcp_private, key_path)
            shutil.copy2(mcp_public, Path(f"{key_path}.pub"))
            key_path.chmod(0o600)
        else:
            result = subprocess.run(
                ["ssh-keygen", "-t", "rsa", "-b", "4096", "-f", str(key_path),
                 "-N", "", "-C", f"aicodinggym-{user_id}"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to generate SSH key: {result.stderr}")

    pub_key_path = Path(f"{key_path}.pub")
    public_key = pub_key_path.read_text().strip()
    return key_path, public_key


def run_git_command(cmd: str, cwd: str, key_path: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Execute a git command with optional SSH key configuration."""
    env = os.environ.copy()
    if key_path:
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=env)


def clone_repo(repo_url: str, branch: str, dest_name: str,
               workspace: str, key_path: Path) -> tuple[bool, str]:
    """Clone a repo branch into workspace/dest_name.

    Returns (success, message).
    """
    problem_dir = Path(workspace) / dest_name

    if problem_dir.exists():
        # Check if already on the correct branch
        result = run_git_command("git rev-parse --abbrev-ref HEAD", str(problem_dir))
        if result.returncode == 0 and result.stdout.strip() == branch:
            pull = run_git_command(f"git pull origin {branch}", str(problem_dir), key_path)
            if pull.returncode != 0:
                return False, f"Git pull failed:\n{pull.stderr}"
            return True, f"Already exists. Updated to latest version.\nRepository: {problem_dir}\nBranch: {branch}"
        return False, (
            f"Directory {problem_dir} already exists with different content.\n"
            "Remove it first or use --workspace-dir to specify a different location."
        )

    cmd = f"git clone --single-branch --branch {branch} --depth 1 {repo_url} {dest_name}"
    result = run_git_command(cmd, workspace, key_path)

    if result.returncode != 0:
        return False, f"Git clone failed:\n{result.stderr}\nMake sure the branch '{branch}' exists in the repository."

    return True, f"Cloned to: {problem_dir}\nBranch: {branch}"


def add_commit_push(problem_dir: str, branch: str, key_path: Path,
                    message: str, force: bool = False) -> tuple[bool, str, str]:
    """Stage, commit, and push changes.

    Returns (success, message, commit_hash).
    """
    pdir = Path(problem_dir)

    # Stage all changes except .github
    result = run_git_command("git add -A -- . ':(exclude).github'", str(pdir))
    if result.returncode != 0:
        return False, f"Git add failed:\n{result.stderr}", ""

    # Check for staged changes
    status = run_git_command("git diff --cached --name-only", str(pdir))
    if not status.stdout.strip():
        return False, "No changes to commit. Your working directory is clean.", ""

    # Commit
    safe_msg = message.replace('"', '\\"')
    result = run_git_command(f'git commit -m "{safe_msg}"', str(pdir))
    if result.returncode != 0:
        return False, f"Git commit failed:\n{result.stderr}", ""

    # Get commit hash
    hash_result = run_git_command("git rev-parse HEAD", str(pdir))
    commit_hash = hash_result.stdout.strip()

    # Push
    push_flag = "--force-with-lease " if force else ""
    result = run_git_command(f"git push {push_flag}origin {branch}", str(pdir), key_path)
    if result.returncode != 0:
        return False, f"Git push failed:\n{result.stderr}", commit_hash

    return True, "Committed and pushed successfully.", commit_hash


def reset_to_setup_commit(problem_dir: str) -> tuple[bool, str]:
    """Reset repo to the original 'Setup SWE-bench instance:' commit.

    Returns (success, message).
    """
    log_result = run_git_command("git log --format=%H:%s --reverse", problem_dir)
    if log_result.returncode != 0:
        return False, f"Git log failed:\n{log_result.stderr}"

    setup_prefix = "Setup SWE-bench instance:"
    setup_commit = None
    for line in log_result.stdout.splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        commit_hash, subject = parts[0].strip(), parts[1].strip()
        if subject.startswith(setup_prefix):
            setup_commit = commit_hash
            break

    if not setup_commit:
        return False, (
            f"Could not find the original setup commit.\n"
            f"Expected a commit message starting with '{setup_prefix}'."
        )

    reset = run_git_command(f"git reset --hard {setup_commit}", problem_dir)
    if reset.returncode != 0:
        return False, f"Git reset failed:\n{reset.stderr}"

    clean = run_git_command("git clean -fd", problem_dir)
    if clean.returncode != 0:
        return False, f"Git clean failed:\n{clean.stderr}"

    return True, f"Reset to setup commit {setup_commit[:8]}.\nLocal changes discarded and untracked files removed."


def check_tool_installed(tool_name: str) -> bool:
    """Check if a CLI tool is available on PATH."""
    return shutil.which(tool_name) is not None
