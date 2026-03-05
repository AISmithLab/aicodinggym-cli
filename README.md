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

# 2. SWE-bench: fetch, solve, submit
aicodinggym swe fetch django__django-10097
# ... edit code to fix the issue ...
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
