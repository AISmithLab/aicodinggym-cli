"""Tests for the Entire logging integration (entire_logging.py).

These exercise the git-level behaviour directly and do NOT require the `entire`
binary: we simulate Entire's `entire/checkpoints/v1` branch by hand, then verify
metadata injection, unique non-overwriting branches, and the MLE code push.
"""

import json
import subprocess

import pytest

from aicodinggym import entire_logging as el


def _git(args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@t", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )


def _init_repo_with_session(repo):
    """A git repo whose entire/checkpoints/v1 branch holds a fake session."""
    _git(["init", "-q", "-b", "main", "."], repo)
    (repo / "code.py").write_text("print('hi')\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "work"], repo)
    _git(["checkout", "-q", "-b", el.CHECKPOINT_BRANCH], repo)
    (repo / "session.json").write_text('{"prompt": "fix the bug"}')
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "checkpoint"], repo)
    _git(["checkout", "-q", "main"], repo)


def _bare_remote(tmp_path, name="remote.git"):
    bare = tmp_path / name
    _git(["init", "--bare", "-q", str(bare)], tmp_path)
    return bare


def _remote_branches(bare):
    out = _git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], bare)
    return set(out.stdout.split())


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_safe_ref_sanitizes_illegal_characters():
    assert el._safe_ref("spaceship titanic") == "spaceship_titanic"
    assert el._safe_ref("a..b") == "a_b"
    assert el._safe_ref("we:ird~name^") == "we_ird_name_"
    assert el._safe_ref("/leading/") == "leading"


def test_logs_branch_with_and_without_suffix():
    assert el.logs_branch("swe", "django__django-10097") == \
        "aicodinggym-logs/swe/django__django-10097"
    assert el.logs_branch("mle", "spaceship-titanic", "20260603T000000Z-abc123") == \
        "aicodinggym-logs/mle/spaceship-titanic/20260603T000000Z-abc123"


def test_new_stamp_is_unique():
    stamps = {el.new_stamp() for _ in range(50)}
    assert len(stamps) == 50


# ── ensure_git_repo / commit_workspace ───────────────────────────────────────

def test_ensure_git_repo_creates_repo_and_ignores(tmp_path):
    ws = tmp_path / "comp"
    ws.mkdir()
    ok, _ = el.ensure_git_repo(ws)
    assert ok
    assert (ws / ".git").is_dir()
    ignore = (ws / ".gitignore").read_text()
    for pat in ("data/", "*.pkl", "*.pt", "__pycache__/", "checkpoints/", "*.zip"):
        assert pat in ignore
    # has an initial commit (HEAD resolves)
    assert _git(["rev-parse", "HEAD"], ws).returncode == 0


def test_ensure_git_repo_is_noop_when_already_a_repo(tmp_path):
    ws = tmp_path / "comp"
    ws.mkdir()
    _git(["init", "-q", "."], ws)
    ok, msg = el.ensure_git_repo(ws)
    assert ok and "already" in msg


def test_commit_workspace_excludes_heavy_artifacts(tmp_path):
    ws = tmp_path / "comp"
    ws.mkdir()
    el.ensure_git_repo(ws)
    (ws / "data").mkdir()
    (ws / "data" / "train.csv").write_text("x,y\n1,2\n")   # dataset -> ignored
    (ws / "model.pkl").write_bytes(b"\x00" * 100)           # weights -> ignored
    (ws / "solution.py").write_text("print('model')\n")     # code -> kept
    (ws / "submission.csv").write_text("id,pred\n1,0\n")    # prediction -> kept

    assert el.commit_workspace(ws, "MLE submission: comp") is True
    tracked = _git(["ls-tree", "-r", "--name-only", "HEAD"], ws).stdout.split()
    assert "solution.py" in tracked
    assert "submission.csv" in tracked
    assert "model.pkl" not in tracked
    assert not any(f.startswith("data/") for f in tracked)


# ── push_branch (MLE code) ───────────────────────────────────────────────────

def test_push_branch_force_overwrites_stable_branch(tmp_path):
    # The MLE code branch is named after the competition and force-pushed, so the
    # latest submission wins (and `mle restore` has a predictable name to pull).
    ws = tmp_path / "comp"
    ws.mkdir()
    el.ensure_git_repo(ws)
    (ws / "solution.py").write_text("v1\n")
    el.commit_workspace(ws, "submit 1")
    bare = _bare_remote(tmp_path)

    ok1, b1 = el.push_branch(ws, remote_url=str(bare),
                             dest_branch="spaceship-titanic", key_path=None, force=True)
    (ws / "solution.py").write_text("v2\n")
    el.commit_workspace(ws, "submit 2")
    ok2, b2 = el.push_branch(ws, remote_url=str(bare),
                             dest_branch="spaceship-titanic", key_path=None, force=True)

    assert ok1 and ok2 and b1 == b2 == "spaceship-titanic"
    assert _remote_branches(bare) == {"spaceship-titanic"}  # one stable branch
    assert _git(["show", "spaceship-titanic:solution.py"], bare).stdout == "v2\n"


# ── ensure_commit_linking (suppresses Entire's per-commit prompt) ─────────────

def test_ensure_commit_linking_sets_always_and_merges(tmp_path):
    repo = tmp_path / "r"
    (repo / ".entire").mkdir(parents=True)
    el.ensure_commit_linking(repo)
    settings = repo / ".entire" / "settings.local.json"
    assert json.loads(settings.read_text())["commit_linking"] == "always"

    # Preserves unrelated keys and is idempotent.
    settings.write_text(json.dumps({"telemetry": False}))
    el.ensure_commit_linking(repo)
    data = json.loads(settings.read_text())
    assert data == {"telemetry": False, "commit_linking": "always"}


def test_ensure_commit_linking_noop_without_entire_dir(tmp_path):
    el.ensure_commit_linking(tmp_path)  # no .entire -> must not create anything
    assert not (tmp_path / ".entire").exists()


def test_push_branch_pushes_to_unique_branch_without_overwrite(tmp_path):
    ws = tmp_path / "comp"
    ws.mkdir()
    el.ensure_git_repo(ws)
    (ws / "solution.py").write_text("v1\n")
    el.commit_workspace(ws, "submit 1")
    bare = _bare_remote(tmp_path)

    ok1, b1 = el.push_branch(ws, remote_url=str(bare),
                             dest_branch="spaceship-titanic/stamp1", key_path=None)
    (ws / "solution.py").write_text("v2\n")
    el.commit_workspace(ws, "submit 2")
    ok2, b2 = el.push_branch(ws, remote_url=str(bare),
                             dest_branch="spaceship-titanic/stamp2", key_path=None)

    assert ok1 and ok2 and b1 != b2
    # Both submissions are preserved on the remote — neither overwrote the other.
    assert {"spaceship-titanic/stamp1", "spaceship-titanic/stamp2"} <= _remote_branches(bare)


# ── upload (AI logs + metadata) ──────────────────────────────────────────────

def test_upload_pushes_unique_branch_with_metadata(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_session(repo)
    bare = _bare_remote(tmp_path)

    ok, branch = el.upload(
        repo, remote_url=str(bare), benchmark="swe",
        problem_id="django__django-10097", user_id="alice",
        key_path=None, tool="claude-code", cli_version="0.6.0",
        submission_stamp="20260603T000000Z-aaaa1111",
    )
    assert ok
    assert branch == "aicodinggym-logs/swe/django__django-10097/20260603T000000Z-aaaa1111"

    files = _git(["ls-tree", "-r", "--name-only", branch], bare).stdout.split()
    assert el.METADATA_FILENAME in files
    assert "session.json" in files  # original captured session carried over

    meta = json.loads(_git(["show", f"{branch}:{el.METADATA_FILENAME}"], bare).stdout)
    assert meta["problem_id"] == "django__django-10097"
    assert meta["benchmark"] == "swe"
    assert meta["user_id"] == "alice"
    assert meta["submission_id"] == "20260603T000000Z-aaaa1111"


def test_upload_twice_does_not_overwrite_previous_logs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_session(repo)
    bare = _bare_remote(tmp_path)

    _, b1 = el.upload(repo, remote_url=str(bare), benchmark="swe",
                      problem_id="p1", user_id="u", submission_stamp="s1")
    _, b2 = el.upload(repo, remote_url=str(bare), benchmark="swe",
                      problem_id="p1", user_id="u", submission_stamp="s2")
    branches = _remote_branches(bare)
    assert b1 in branches and b2 in branches and b1 != b2


def test_upload_returns_false_without_sessions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main", "."], repo)
    (repo / "f").write_text("x")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "c"], repo)  # no checkpoint branch
    bare = _bare_remote(tmp_path)

    ok, msg = el.upload(repo, remote_url=str(bare), benchmark="swe",
                        problem_id="p", user_id="u")
    assert not ok and "no captured sessions" in msg


def test_has_sessions_reflects_checkpoint_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main", "."], repo)
    (repo / "f").write_text("x")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "c"], repo)
    assert el.has_sessions(repo) is False
    _git(["branch", el.CHECKPOINT_BRANCH], repo)
    assert el.has_sessions(repo) is True
