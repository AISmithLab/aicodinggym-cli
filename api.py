"""HTTP API client for the AI Coding Gym backend at aicodinggym.com."""

import os

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


def cr_submit_review(user_id: str, problem_id: str, review: str) -> dict:
    """Submit a code review."""
    return _post("code-review-submit", {
        "user_id": user_id,
        "problem_id": problem_id,
        "review": review,
    })


def mlebench_download_info(user_id: str, competition_id: str, dest_path: str) -> None:
    """Download dataset for an MLE-bench competition directly to dest_path."""
    resp = _get(f"competitions/{competition_id}/download", stream=True)
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


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


def mlebench_submit_csv(user_id: str, competition_id: str, csv_path: str) -> dict:
    """Upload a prediction CSV for an MLE-bench competition."""
    try:
        with open(csv_path, "rb") as f:
            resp = requests.post(
                f"{API_BASE}/competitions/{competition_id}/submit",
                data={"user_id": user_id, "competition_id": competition_id},
                files={"file": (f.name, f, "text/csv")},
                timeout=60,
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
