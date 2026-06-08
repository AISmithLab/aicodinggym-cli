"""Tests for git_ops.restore_branch — `aicodinggym mle restore`'s workhorse.

Verifies it pulls tracked code back from a remote branch while leaving the
already-present (gitignored) dataset untouched, and guards uncommitted work.
"""

import subprocess

from aicodinggym import git_ops


def _git(args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )


def _seed_remote_with_code(tmp_path):
    """A bare remote holding branch 'comp' with solution.py at v1 (data/ ignored)."""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "comp", "."], src)
    (src / ".gitignore").write_text("data/\n")
    (src / "solution.py").write_text("v1\n")
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "submit"], src)

    bare = tmp_path / "remote.git"
    _git(["init", "--bare", "-q", str(bare)], tmp_path)
    _git(["push", "-q", str(bare), "comp"], src)
    return bare


def test_restore_into_new_dir_with_existing_dataset(tmp_path):
    bare = _seed_remote_with_code(tmp_path)
    target = tmp_path / "ws" / "comp"
    target.mkdir(parents=True)
    (target / "data").mkdir()
    (target / "data" / "train.csv").write_text("keep me\n")  # gitignored dataset

    ok, msg = git_ops.restore_branch(str(bare), "comp", str(target), key_path=None)

    assert ok, msg
    assert (target / "solution.py").read_text() == "v1\n"      # code restored
    assert (target / "data" / "train.csv").read_text() == "keep me\n"  # dataset kept


def test_restore_refuses_to_clobber_uncommitted_without_force(tmp_path):
    bare = _seed_remote_with_code(tmp_path)
    target = tmp_path / "comp"
    git_ops.restore_branch(str(bare), "comp", str(target), key_path=None)

    (target / "solution.py").write_text("LOCAL WORK\n")  # uncommitted change
    ok, msg = git_ops.restore_branch(str(bare), "comp", str(target), key_path=None)
    assert not ok and "uncommitted" in msg
    assert (target / "solution.py").read_text() == "LOCAL WORK\n"  # untouched

    ok2, _ = git_ops.restore_branch(str(bare), "comp", str(target), key_path=None, force=True)
    assert ok2 and (target / "solution.py").read_text() == "v1\n"  # overwritten


def test_restore_reports_missing_branch(tmp_path):
    bare = _seed_remote_with_code(tmp_path)
    target = tmp_path / "comp"
    ok, msg = git_ops.restore_branch(str(bare), "does-not-exist", str(target), key_path=None)
    assert not ok and "fetch" in msg.lower()
