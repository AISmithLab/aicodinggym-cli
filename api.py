"""HTTP API client for the AI Coding Gym backend at aicodinggym.com."""

import gzip
import os
from pathlib import Path
from urllib.parse import quote

import requests

API_BASE = os.environ.get("AICODINGGYM_API_BASE", "https://aicodinggym.com/api")
TIMEOUT = 30


class APIError(Exception):
    """Raised when an API call fails."""
    pass


def _post(endpoint: str, payload: dict, timeout: int = TIMEOUT) -> dict:
    """Make a POST request to the API and return parsed JSON."""
    url = f"{API_BASE}/{endpoint}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        raise APIError(
            f"Cannot connect to {API_BASE}.\n"
            "Check your internet connection and try again."
        )
    except requests.Timeout:
        raise APIError(f"Request to {url} timed out after {timeout}s.")
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.json().get("detail", e.response.text)
        except Exception:
            body = e.response.text
        raise APIError(f"API error (HTTP {e.response.status_code}): {body}")
    except requests.RequestException as e:
        raise APIError(f"Request failed: {e}")


def _get(endpoint: str, timeout: int = TIMEOUT, stream: bool = False) -> requests.Response:
    """Make a GET request to the API and return the raw response."""
    url = f"{API_BASE}/{endpoint}"
    try:
        resp = requests.get(url, timeout=timeout, stream=stream)
        resp.raise_for_status()
        return resp
    except requests.ConnectionError:
        raise APIError(
            f"Cannot connect to {API_BASE}.\n"
            "Check your internet connection and try again."
        )
    except requests.Timeout:
        raise APIError(f"Request to {url} timed out after {timeout}s.")
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.json().get("detail", e.response.text)
        except Exception:
            body = e.response.text
        raise APIError(f"API error (HTTP {e.response.status_code}): {body}")
    except requests.RequestException as e:
        raise APIError(f"Request failed: {e}")


def configure(user_id: str, public_key: str) -> dict:
    """Register public key with server. Returns {'repo_name': ...}."""
    return _post("configure", {"user_id": user_id, "public_key": public_key})


def fetch_problem(user_id: str, problem_id: str) -> dict:
    """Fetch problem info. Returns {'branch_name': ..., 'repo_url': ..., 'message': ...}."""
    return _post("fetch-problem", {"user_id": user_id, "problem_id": problem_id})


def submit_notification(problem_id: str, user_id: str, commit_hash: str,
                        branch: str, commit_message: str, timestamp: str) -> dict:
    """Notify backend of a submission."""
    return _post("submissions", {
        "problem_id": problem_id,
        "user_id": user_id,
        "commit_hash": commit_hash,
        "branch": branch,
        "commit_message": commit_message,
        "timestamp": timestamp,
    })


def fetch_pr(user_id: str, problem_id: str) -> dict:
    """Fetch CR problem info. Returns {'base_branch': ..., 'head_branch': ..., 'repo_url': ...}."""
    return _post("code-review-fetch", {"user_id": user_id, "problem_id": problem_id})


def cr_submit_review(user_id: str, problem_id: str, review: str) -> dict:
    """Submit a code review."""
    return _post("code-review-submit", {
        "user_id": user_id,
        "problem_id": problem_id,
        "review": review,
    })


def mlebench_download_open(competition_id: str, chunk_size: int = 1 << 16):
    """Open a streaming download for an MLE-bench dataset.

    Returns (total_bytes, chunk_iterator). ``total_bytes`` is the server's
    Content-Length, or 0 if it isn't advertised. Lets the caller drive a
    progress bar while writing chunks to disk.
    """
    resp = _get(f"competitions/{competition_id}/download", stream=True)
    total = int(resp.headers.get("Content-Length") or 0)
    return total, resp.iter_content(chunk_size=chunk_size)


def mlebench_download_file(url: str, dest_path: str, timeout: int = 300) -> None:
    """Download a file from the given URL to dest_path with progress."""
    try:
        resp = requests.get(url, stream=True, timeout=timeout)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    except requests.RequestException as e:
        raise APIError(f"Download failed: {e}")


# ── Guardrail Gym Level 3: "Assistant Pro" live red-team sessions ─────────────
# These drive the /api/guardrails/live/* API. All side-effects are simulated by
# the server (nothing is really sent/executed); the judge is deterministic.

def guardrail_info(world: str | None = None) -> dict:
    """Capability discovery: tools, objectives, medal thresholds, limits."""
    path = "guardrails/live/info"
    if world:
        path += f"?world={quote(world)}"
    return _get(path).json()


def guardrail_start(user_id: str, world: str | None = None) -> dict:
    """Create a new live session. Returns {'sessionId', 'model', 'status', ...}."""
    body: dict = {"userId": user_id}
    if world:
        body["world"] = world
    return _post("guardrails/live/session", body)


def guardrail_catalog(world: str | None = None) -> dict:
    """Plant palette (grouped by surface) plus the victim routines you can run."""
    path = "guardrails/live/catalog"
    if world:
        path += f"?world={quote(world)}"
    return _get(path).json()


def guardrail_plant(session_id: str, plant_kind: str, args: dict) -> dict:
    """Seed content into the app (indirect-injection setup) via a real feature."""
    return _post(
        "guardrails/live/plant",
        {"sessionId": session_id, "plantKind": plant_kind, "args": args or {}},
    )


def guardrail_status(session_id: str) -> dict:
    """Full session state: status, medal, captures, app view, transcript."""
    return _get(f"guardrails/live/session/{quote(session_id)}").json()


def guardrail_latest(user_id: str) -> dict:
    """The user's most recent session. Returns {'session': <state>|None}."""
    return _get(f"guardrails/live/latest?userId={quote(user_id)}").json()


def guardrail_reset(session_id: str) -> dict:
    """Reseed the app for a session (fresh inbox/files/scopes, same session id)."""
    return _post(f"guardrails/live/session/{quote(session_id)}/reset", {})


def guardrail_finish(session_id: str) -> dict:
    """Close a session. Returns {'sessionId', 'status', 'medal', 'captures'}."""
    return _post(f"guardrails/live/session/{quote(session_id)}/finish", {})


def mlebench_submit_csv(user_id: str, competition_id: str, csv_path: str) -> dict:
    """Upload a prediction CSV for an MLE-bench competition."""
    try:
        csv_name = Path(csv_path).name
        with open(csv_path, "rb") as f:
            compressed = gzip.compress(f.read())
        resp = requests.post(
            f"{API_BASE}/competitions/{competition_id}/submit",
            data={"user_id": user_id, "competition_id": competition_id},
            files={"file": (csv_name + ".gz", compressed, "application/gzip")},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        raise APIError(
            f"Cannot connect to {API_BASE}.\n"
            "Check your internet connection and try again."
        )
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.json().get("detail", e.response.text)
        except Exception:
            body = e.response.text
        raise APIError(f"API error (HTTP {e.response.status_code}): {body}")
    except requests.RequestException as e:
        raise APIError(f"Request failed: {e}")
