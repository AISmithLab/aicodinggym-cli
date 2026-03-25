"""Git and SSH key operations for AI Coding Gym CLI."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import ensure_config_dir


def _find_git_ssh() -> str | None:
    """On Windows, find Git for Windows' bundled ssh.exe.

    Windows may have two SSH binaries on PATH: the built-in OpenSSH
    (C:\\Windows\\System32\\OpenSSH\\ssh.exe) and Git for Windows' MSYS2
    ssh (C:\\Program Files\\Git\\usr\\bin\\ssh.exe).  System32 is usually
    first on PATH, so an unqualified 'ssh' resolves to Windows OpenSSH,
    which can trigger GUI credential dialogs or deadlock when stdout is
    captured.  This function returns the full path to Git's bundled ssh
    so we can reference it explicitly in GIT_SSH_COMMAND.
    """
    if sys.platform != "win32":
        return None
    git_path = shutil.which("git")
    if not git_path:
        return None
    # Walk up from git.exe to find the Git root containing usr/bin/ssh.exe.
    # Handles cmd/, bin/, and mingw64/bin/ layouts.
    candidate = Path(git_path).resolve().parent
    for _ in range(4):
        ssh = candidate / "usr" / "bin" / "ssh.exe"
        if ssh.exists():
            return str(ssh).replace("\\", "/")
        candidate = candidate.parent
    return None


def _validate_git_ref(name: str, label: str) -> None:
    """Raise ValueError if name contains suspicious shell metacharacters."""
    if re.search(r'[;&|`$(){}]', name):
        raise ValueError(f"Invalid {label}: {name!r}")


def _restrict_key_permissions(key_path: Path) -> None:
    """Restrict an SSH private key file to owner-only access.

    On Unix/macOS: chmod 600 (read/write owner only).
    On Windows: uses icacls to remove inherited permissions and grant
    full control only to the current user.  SSH clients on both platforms
    refuse to use a key whose permissions are too open.
    """
    if sys.platform == "win32":
        # Remove inherited ACLs, then grant only the current user full control.
        # (F) = Full control, matching chmod 0o600 (owner read+write).
        key_str = str(key_path)
        username = os.environ.get("USERNAME", "")
        if username:
            subprocess.run(
                ["icacls", key_str, "/inheritance:r",
                 "/grant:r", f"{username}:(F)"],
                capture_output=True,
            )
    else:
        key_path.chmod(0o600)


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
            _restrict_key_permissions(key_path)
        else:
            if not shutil.which("ssh-keygen"):
                raise RuntimeError(
                    "ssh-keygen is not installed or not on PATH.\n"
                    "On Windows, install Git for Windows (https://git-scm.com) "
                    "which includes ssh-keygen, or use the OpenSSH optional feature."
                )
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


def run_git_command(cmd: list[str], cwd: str, key_path: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Execute a git command with optional SSH key configuration.

    cmd must be a list of arguments (e.g. ["git", "status"]).
    """
    env = os.environ.copy()
    if key_path:
        # Quote the key path in case it contains spaces (common on Windows).
        # Use forward slashes — works on all platforms and avoids backslash escaping.
        quoted_key = str(key_path).replace("\\", "/")
        # On Windows, use Git for Windows' bundled ssh to avoid Windows native
        # OpenSSH which can trigger GUI credential dialogs or deadlock when
        # stdout is captured.  Falls back to bare "ssh" if not found.
        ssh_bin = _find_git_ssh() or "ssh"
        # Always use /dev/null for UserKnownHostsFile.  On macOS/Linux this is
        # the native null device.  On Windows, Git for Windows bundles MSYS2's
        # ssh which translates /dev/null correctly.  Using os.devnull ("nul")
        # would break MSYS2's ssh which treats "nul" as a literal filename.
        # BatchMode=yes prevents any interactive prompts (password, passphrase)
        # that would cause a hang when stdout/stderr are captured.
        env["GIT_SSH_COMMAND"] = (
            f'"{ssh_bin}" -i "{quoted_key}" '
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o BatchMode=yes"
        )

    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)


def clone_repo(repo_url: str, branch: str, dest_name: str,
               workspace: str, key_path: Path) -> tuple[bool, str]:
    """Clone a repo branch into workspace/dest_name.

    Returns (success, message).
    """
    problem_dir = Path(workspace) / dest_name

    if problem_dir.exists():
        # Check if already on the correct branch
        result = run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], str(problem_dir))
        if result.returncode == 0 and result.stdout.strip() == branch:
            pull = run_git_command(["git", "pull", "origin", branch], str(problem_dir), key_path)
            if pull.returncode != 0:
                return False, f"Git pull failed:\n{pull.stderr}"
            return True, f"Already exists. Updated to latest version.\nRepository: {problem_dir}\nBranch: {branch}"
        return False, (
            f"Directory {problem_dir} already exists with different content.\n"
            "Remove it first or use --workspace-dir to specify a different location."
        )

    cmd = ["git", "clone", "--single-branch", "--branch", branch, "--depth", "1", repo_url, dest_name]
    result = run_git_command(cmd, workspace, key_path)

    if result.returncode != 0:
        return False, f"Git clone failed:\n{result.stderr}\nMake sure the branch '{branch}' exists in the repository."

    return True, f"Cloned to: {problem_dir}\nBranch: {branch}"


def clone_repo_cr(repo_url: str, base_branch: str, head_branch: str,
                  dest_name: str, workspace: str,
                  key_path: Optional[Path] = None) -> tuple[bool, str]:
    """Clone a code review repo with both base and head branches.

    Clones the base branch first (shallow), then fetches the head branch.
    Returns (success, message).
    """
    _validate_git_ref(base_branch, "base_branch")
    _validate_git_ref(head_branch, "head_branch")
    _validate_git_ref(repo_url, "repo_url")
    _validate_git_ref(dest_name, "dest_name")

    problem_dir = Path(workspace) / dest_name

    if problem_dir.exists():
        # Already cloned — fetch latest for both branches
        for branch in (base_branch, head_branch):
            result = run_git_command(["git", "fetch", "origin", branch], str(problem_dir), key_path)
            if result.returncode != 0:
                return False, f"Git fetch failed for {branch}:\n{result.stderr}"
            result = run_git_command(["git", "branch", "-f", branch, "FETCH_HEAD"], str(problem_dir))
            if result.returncode != 0:
                return False, f"Failed to update branch {branch}:\n{result.stderr}"
        result = run_git_command(["git", "checkout", head_branch], str(problem_dir))
        if result.returncode != 0:
            return False, f"Failed to checkout head branch '{head_branch}':\n{result.stderr}"
        return True, (
            f"Already exists. Updated both branches.\n"
            f"Repository: {problem_dir}\n"
            f"Branches: {base_branch}, {head_branch}"
        )

    # Clone base branch (shallow); depth 50 needed for diffing between branches
    cmd = ["git", "clone", "--single-branch", "--branch", base_branch, "--depth", "50", repo_url, dest_name]
    result = run_git_command(cmd, workspace, key_path)
    if result.returncode != 0:
        return False, f"Git clone failed:\n{result.stderr}"

    # Fetch head branch
    result = run_git_command(["git", "fetch", "origin", head_branch], str(problem_dir), key_path)
    if result.returncode != 0:
        return False, f"Failed to fetch head branch '{head_branch}':\n{result.stderr}"

    # Create local head branch tracking the fetched ref
    result = run_git_command(["git", "branch", "-f", head_branch, "FETCH_HEAD"], str(problem_dir))
    if result.returncode != 0:
        return False, f"Failed to create branch {head_branch}:\n{result.stderr}"

    # Check out head branch so the user starts on the code being reviewed
    result = run_git_command(["git", "checkout", head_branch], str(problem_dir))
    if result.returncode != 0:
        return False, f"Failed to checkout head branch '{head_branch}':\n{result.stderr}"

    return True, (
        f"Cloned to: {problem_dir}\n"
        f"Branches: {base_branch}, {head_branch}"
    )


def add_commit_push(problem_dir: str, branch: str, key_path: Path,
                    message: str, force: bool = False) -> tuple[bool, str, str]:
    """Stage, commit, and push changes.

    Returns (success, message, commit_hash).
    """
    pdir = Path(problem_dir)

    # Stage all changes except dotfiles/dotdirs and markdown files
    result = run_git_command([
        "git", "add", "-A", "--", ".",
        ":(exclude).*",
        ":(exclude)*.md",
    ], str(pdir))
    if result.returncode != 0:
        return False, f"Git add failed:\n{result.stderr}", ""

    # Check for staged changes
    status = run_git_command(["git", "diff", "--cached", "--name-only"], str(pdir))
    if not status.stdout.strip():
        return False, "No changes to commit. Your working directory is clean.", ""

    # Commit — pass message directly as a list arg; no shell escaping needed
    result = run_git_command(["git", "commit", "-m", message], str(pdir))
    if result.returncode != 0:
        return False, f"Git commit failed:\n{result.stderr}", ""

    # Get commit hash
    hash_result = run_git_command(["git", "rev-parse", "HEAD"], str(pdir))
    commit_hash = hash_result.stdout.strip()

    # Push
    push_cmd = ["git", "push"]
    if force:
        push_cmd.append("--force-with-lease")
    push_cmd += ["origin", branch]
    result = run_git_command(push_cmd, str(pdir), key_path)
    if result.returncode != 0:
        return False, f"Git push failed:\n{result.stderr}", commit_hash

    return True, "Committed and pushed successfully.", commit_hash


def reset_to_setup_commit(problem_dir: str) -> tuple[bool, str]:
    """Reset repo to the original 'Setup SWE-bench instance:' commit.

    Returns (success, message).
    """
    log_result = run_git_command(["git", "log", "--format=%H:%s", "--reverse"], problem_dir)
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

    reset = run_git_command(["git", "reset", "--hard", setup_commit], problem_dir)
    if reset.returncode != 0:
        return False, f"Git reset failed:\n{reset.stderr}"

    clean = run_git_command(["git", "clean", "-fd"], problem_dir)
    if clean.returncode != 0:
        return False, f"Git clean failed:\n{clean.stderr}"

    return True, f"Reset to setup commit {setup_commit[:8]}.\nLocal changes discarded and untracked files removed."


def check_tool_installed(tool_name: str) -> bool:
    """Check if a CLI tool is available on PATH."""
    return shutil.which(tool_name) is not None
