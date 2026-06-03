# aicodinggym-cli

CLI tool for the [AI Coding Gym](https://aicodinggym.com) platform.
Supports three benchmarks: **SWE-bench** (code bug fixes), **MLE-bench** (ML competitions), and **Code Review** challenges.

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

# 4. Code Review: fetch, review, submit
aicodinggym cr fetch keycloak-0008
# ... read diff.patch, write your review in review.md ...
aicodinggym cr submit keycloak-0008 -f review.md
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
| `--upload-logs` / `--no-upload-logs` | No | Pre-set consent for uploading de-identified AI session logs for research. If unset, you're asked once on your first submit. |

On first run, if the [Entire](https://entire.io) CLI isn't installed, `configure` offers to install it (used for [AI workflow logging](#ai-workflow-logging-entire)).

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
| `--upload-logs` / `--no-upload-logs` | Override consent for uploading de-identified AI session logs |
| `--logs-remote` | Git URL to push AI session logs to (defaults to this problem's repo) |

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
| `--upload-logs` / `--no-upload-logs` | No | Override consent for uploading de-identified AI session logs |
| `--logs-remote` | No | Git URL to push AI session logs to (defaults to your submission repo) |

---

### `aicodinggym cr` — Code Review Commands

#### `aicodinggym cr fetch PROBLEM_ID`

Download the PR diff and create a `review.md` template.

```
aicodinggym cr fetch PROBLEM_ID [--user-id ID] [--workspace-dir DIR]
```

Creates in `<workspace>/<problem_id>/`:
- `diff.patch` — the full diff between base and head branches
- `review.md` — template to fill in your review (only created if not already present)

#### `aicodinggym cr submit PROBLEM_ID`

Submit your code review.

```
aicodinggym cr submit PROBLEM_ID -f review.md [--user-id ID]
aicodinggym cr submit PROBLEM_ID -m "Inline review text"
echo "My review" | aicodinggym cr submit PROBLEM_ID
```

| Option | Description |
|---|---|
| `-f, --file` | Path to a file containing your review (e.g. `review.md`) |
| `-m, --message` | Inline review text |
| stdin | Pipe review text from stdin |
| `--upload-logs` / `--no-upload-logs` | Override consent for uploading de-identified AI session logs |
| `--logs-remote` | Git URL to push AI session logs to (defaults to your submission repo) |

---

## AI Workflow Logging (Entire)

AI Coding Gym can optionally capture **how** a solution was produced — the AI
agent prompts, responses, tool calls and files touched — for research. This is
powered by the [Entire](https://entire.io) CLI, which hooks into git and stores
sessions on a separate `entire/checkpoints/v1` branch (it never adds commits to
your working branch).

**Consent first.** Capture is **local only** until you opt in. The first time
you `submit` (unless already configured), you're asked once:

> AI Coding Gym can upload this AI coding session (prompts, responses, files
> changed) for research only. Data is de-identified/anonymized before use.

Your choice is saved in `~/.aicodinggym/config.json`. Set it ahead of time with
`aicodinggym configure --upload-logs` / `--no-upload-logs`, or override per
submit with `--upload-logs` / `--no-upload-logs`. In non-interactive sessions
with no recorded choice, nothing is uploaded.

**One repo, many branches.** All three benchmarks log to your **single** repo
(the writable per-user repo — one repo, many branches), so a log is identified
by its branch name even though the repo holds many problems. SWE `fetch` records
that repo URL (`submission_repo_url`) so CR/MLE reuse it; the cloned CR PR repo
is read-only and is never used as a target. Override with `--logs-remote` or the
`AICODINGGYM_LOGS_REMOTE` env var.

**How it works**

1. `swe fetch` / `cr fetch` / `mle download` installs Entire's hooks so your
   session is captured locally as you work. (MLE workspaces aren't git repos, so
   a lightweight one is initialised — this also lets your solution code be
   pushed on submit. The dataset under `data/` is gitignored.)
   If the `entire` CLI isn't installed, you're pointed at `aicodinggym configure`
   (which offers to install it) instead of being silently skipped.
2. On `submit`, after consent, the captured `entire/checkpoints/v1` branch is
   pushed to your repo on a per-problem branch
   `aicodinggym-logs/<benchmark>/<problem_id>`, with an `aicodinggym-meta.json`
   file at the tip (problem id, benchmark, user, tool, timestamp).
3. **MLE also pushes your solution code** (notebooks/scripts/CSV; `data/`
   excluded) to a branch named after the competition (e.g. `spaceship-titanic`),
   gated by the same consent. The prediction CSV still goes to the scoring API as
   before.

**Privacy.** Uploaded data is used solely for research and is
de-identified/anonymized before use. Entire also redacts detected secrets when
writing to the checkpoints branch (best-effort). Logging is entirely optional —
if the `entire` binary isn't installed, fetch/submit work unchanged.

---

## File Structure

```
aicodinggym-cli/
├── __init__.py      # Version
├── cli.py           # Click CLI commands (entry point)
├── config.py        # Config + credentials persistence (~/.aicodinggym/)
├── api.py           # HTTP client for aicodinggym.com/api
├── git_ops.py       # SSH key generation, git clone/commit/push/reset
├── entire_logging.py # Optional AI-session capture/upload via the Entire CLI
└── pyproject.toml   # Package metadata and build config
```

## Configuration Files

| File | Purpose |
|---|---|
| `~/.aicodinggym/config.json` | Global config (user_id, repo_name, key path, workspace, log-upload consent, submission repo URL) |
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
