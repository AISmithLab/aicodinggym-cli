# aicodinggym-cli

CLI tool for the [AI Coding Gym](https://aicodinggym.com) platform.
Supports two benchmarks: **SWE-bench** (code bug fixes) and **MLE-bench** (ML competitions).

**Install:** `pip install aicodinggym-cli`
**Entry point:** `aicodinggym`

---

## Quick Start

```bash
# 1. Configure (one-time setup)
aicodinggym configure --user-id YOUR_USER_ID

# 2. SWE-bench: fetch, solve, test, submit
aicodinggym swe fetch django__django-10097
# ... edit code to fix the issue ...
aicodinggym swe test django__django-10097    # run tests locally (requires Docker + act)
aicodinggym swe submit django__django-10097

# 3. MLE-bench: download, train, submit
aicodinggym mle download spaceship-titanic
# ... train model, generate predictions ...
aicodinggym mle submit spaceship-titanic -F predictions.csv
```

---

## Commands

### `aicodinggym configure`

One-time setup. Generates SSH key, registers with server.

```
aicodinggym configure --user-id USER_ID [--workspace-dir DIR]
```

| Option | Required | Description |
|---|---|---|
| `--user-id` | Yes | Your AI Coding Gym user ID |
| `--workspace-dir` | No | Default workspace directory (default: cwd) |

---

### `aicodinggym swe` — SWE-bench Commands

#### `aicodinggym swe fetch PROBLEM_ID`

Fetch a problem and clone the repo locally.

```
aicodinggym swe fetch PROBLEM_ID [--user-id ID] [--workspace-dir DIR]
```

#### `aicodinggym swe submit PROBLEM_ID`

Commit all changes and push to remote. Notifies backend.

```
aicodinggym swe submit PROBLEM_ID [--message MSG] [--force] [--user-id ID] [--workspace-dir DIR]
```

| Option | Description |
|---|---|
| `--message, -m` | Commit message (auto-generated if omitted) |
| `--force` | Force push with `--force-with-lease` |

#### `aicodinggym swe test PROBLEM_ID`

Run the SWE-bench evaluation tests locally using [nektos/act](https://github.com/nektos/act).
Executes the GitHub Actions workflow from the problem repo on your machine via Docker.

```
aicodinggym swe test PROBLEM_ID [-W WORKFLOW] [--act-args ARGS] [--user-id ID] [--workspace-dir DIR]
```

| Option | Description |
|---|---|
| `-W` | Specific workflow file in `.github/workflows/` (default: all) |
| `--act-args` | Extra arguments passed to `act` (e.g. `'--container-architecture linux/amd64'`) |

**Prerequisites:**
- **Docker** — must be installed and running ([install](https://docs.docker.com/get-docker/))
- **act** — must be installed ([install](https://github.com/nektos/act#installation))
  - macOS: `brew install act`
  - Windows: `choco install act-cli` or `winget install nektos.act`
  - Linux: `curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash`

**Notes:**
- On Apple Silicon, x86_64 emulation is auto-enabled when the workflow requires it (e.g. old Python or platform-specific conda packages). This adds overhead (~4-5 min vs ~2.5 min on native x86_64).
- Output is filtered to show step progress and test results only. Full setup logs (conda, pip) are suppressed.
- A test summary with pass/fail status and elapsed time is printed at the end.

#### `aicodinggym swe reset PROBLEM_ID`

Reset repo to original setup commit. Destructive — discards all local changes.

```
aicodinggym swe reset PROBLEM_ID [--user-id ID] [--workspace-dir DIR]
```

---

### `aicodinggym mle` — MLE-bench Commands

#### `aicodinggym mle download COMPETITION_ID`

Download dataset files as a zip archive.

```
aicodinggym mle download COMPETITION_ID [--user-id ID] [--workspace-dir DIR]
```

| Option | Description |
|---|---|
| `--workspace-dir` | Workspace directory (default: configured workspace) |

Files are saved to `<workspace>/<competition_id>/data/<competition_id>.zip`.

#### `aicodinggym mle submit COMPETITION_ID -F FILE`

Upload prediction CSV for scoring.

```
aicodinggym mle submit COMPETITION_ID -F FILE [--user-id ID] [--message MSG]
```

| Option | Required | Description |
|---|---|---|
| `-F` | Yes | Path to prediction CSV file |
| `--message, -m` | No | Submission description |

---

## File Structure

```
aicodinggym-cli/
├── __init__.py      # Version
├── cli.py           # Click CLI commands (entry point)
├── config.py        # Config + credentials persistence (~/.aicodinggym/)
├── api.py           # HTTP client for aicodinggym.com/api
├── git_ops.py       # SSH key generation, git clone/commit/push/reset
└── pyproject.toml   # Package metadata and build config
```

## Configuration Files

| File | Purpose |
|---|---|
| `~/.aicodinggym/config.json` | Global config (user_id, repo_name, key path, workspace) |
| `~/.aicodinggym/credentials.json` | Per-problem credentials (repo_url, branch, cached after fetch) |
| `~/.aicodinggym/{user_id}_id_rsa` | SSH private key |
| `~/.aicodinggym/{user_id}_id_rsa.pub` | SSH public key |

## Backend API Summary

| Endpoint | Method | Used By |
|---|---|---|
| `/api/configure` | POST | `configure` |
| `/api/fetch-problem` | POST | `swe fetch` |
| `/api/submissions` | POST | `swe submit` |
| `/api/competitions/<id>/download` | GET | `mle download` |
| `/api/competitions/<id>/submit` | POST | `mle submit` |
