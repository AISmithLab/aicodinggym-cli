"""MLE Experiment Logger — track model changes and scores over a competition.

Stores append-only JSONL at <workspace>/<competition_id>/.mle_log.jsonl.
Each line is one LogEntry capturing: summary, model, val/submit scores,
files changed, git diff stats, author (human/ai), and tags.
"""

import csv
import hashlib
import io
import json
import os
import secrets
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


LOG_FILENAME = ".mle_log.jsonl"
_UNCOMMITTED_CAP = 50

_AI_AUTHOR_KEYWORDS = frozenset(
    {"copilot", "claude", "cursor", "aider", "devin", "cody", "gpt", "gemini", "bot"}
)


@dataclass
class LogEntry:
    id: str
    timestamp: str
    author: str
    summary: str
    model: str | None = None
    val_score: dict[str, Any] | None = None
    submit_score: float | None = None
    hyperparams: dict[str, Any] | None = None
    files_changed: list[str] = field(default_factory=list)
    diff_summary: str | None = None
    git_hash: str | None = None
    tags: list[str] = field(default_factory=list)
    event_type: str | None = None  # "checkpoint" | "platform_submission"
    linked_checkpoint_id: str | None = None
    submission_artifact: dict[str, Any] | None = None
    git_provenance: dict[str, Any] | None = None
    api_result: dict[str, Any] | None = None

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json_line(cls, line: str) -> "LogEntry":
        data = json.loads(line)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Author detection (4-priority chain)
# ---------------------------------------------------------------------------

def detect_author(explicit: str | None = None, working_dir: Path | None = None) -> str:
    """Determine whether the current actor is 'human' or 'ai'.

    Priority:
      1. Explicit flag (--author ai / --author human)
      2. AICODINGGYM_AUTHOR env var
      3. Git commit author heuristic
      4. Fallback to 'human'
    """
    if explicit and explicit in ("human", "ai"):
        return explicit

    env_val = os.environ.get("AICODINGGYM_AUTHOR", "").strip().lower()
    if env_val in ("human", "ai"):
        return env_val

    if working_dir:
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%an"],
                cwd=working_dir, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                author_name = result.stdout.strip().lower()
                if any(kw in author_name for kw in _AI_AUTHOR_KEYWORDS):
                    return "ai"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return "human"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_git_diff_summary(working_dir: Path) -> tuple[list[str], str | None, str | None]:
    """Return (files_changed, diff_summary, git_hash) from the working dir.

    Uses git diff HEAD to find uncommitted changes, falls back to
    git diff HEAD~1 if the working tree is clean.

    If ``working_dir`` is inside a larger Git repo but is not the repo root,
    diffs are restricted to paths under that directory so unrelated repo
    changes (e.g. the CLI package root) are not listed.
    """
    files_changed: list[str] = []
    diff_summary: str | None = None
    git_hash: str | None = None

    try:
        wd = working_dir.resolve()
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=wd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        git_cwd = wd
        diff_suffix: list[str] = []
        if top.returncode == 0 and top.stdout.strip():
            root = Path(top.stdout.strip()).resolve()
            git_cwd = root
            if root != wd:
                try:
                    rel = wd.relative_to(root).as_posix()
                    diff_suffix = ["--", f"{rel}/"]
                except ValueError:
                    pass

        # Current hash (from repo root)
        h = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=git_cwd, capture_output=True, text=True, timeout=5,
        )
        if h.returncode == 0:
            git_hash = h.stdout.strip() or None

        # Try uncommitted changes first, then last commit
        for diff_ref in ["HEAD", "HEAD~1"]:
            stat = subprocess.run(
                ["git", "diff", diff_ref, "--stat"] + diff_suffix,
                cwd=git_cwd, capture_output=True, text=True, timeout=10,
            )
            if stat.returncode != 0 or not stat.stdout.strip():
                continue

            lines = stat.stdout.strip().splitlines()
            for line in lines[:-1]:  # skip summary line
                fname = line.split("|")[0].strip()
                if fname:
                    files_changed.append(fname)

            # Parse the summary line for insertions/deletions
            summary_line = lines[-1] if lines else ""
            added = deleted = 0
            for part in summary_line.split(","):
                part = part.strip()
                if "insertion" in part:
                    added = int(part.split()[0])
                elif "deletion" in part:
                    deleted = int(part.split()[0])
            if added or deleted:
                diff_summary = f"+{added} -{deleted}"

            if files_changed:
                break

    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return files_changed, diff_summary, git_hash


def sanitize_api_result(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only JSON-friendly primitives for logging API responses."""

    def clean(x: Any, depth: int = 0) -> Any:
        if depth > 6:
            return None
        if x is None or isinstance(x, (bool, int, float, str)):
            return x
        if isinstance(x, list):
            return [clean(i, depth + 1) for i in x[:100]]
        if isinstance(x, dict):
            out: dict[str, Any] = {}
            for k, v in list(x.items())[:50]:
                if isinstance(k, str):
                    cv = clean(v, depth + 1)
                    if cv is not None or v is None:
                        out[k] = cv
            return out
        return str(x)[:500]

    return clean(data) if isinstance(data, dict) else {}


def capture_mle_provenance(
    working_dir: Path,
    *,
    competition_dir: Path | None = None,
    csv_path: Path | None = None,
) -> dict[str, Any]:
    """Snapshot git state and optional submission CSV for MLE logging."""
    wd = working_dir.resolve()
    comp = (competition_dir or wd).resolve()
    out: dict[str, Any] = {"git_available": False}

    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=wd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if top.returncode != 0 or not top.stdout.strip():
            return out
        root = Path(top.stdout.strip()).resolve()
        out["git_available"] = True
        out["git_toplevel"] = str(root)

        try:
            rel = comp.relative_to(root).as_posix()
            out["competition_path_in_repo"] = rel
        except ValueError:
            out["competition_path_in_repo"] = None

        full = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        short = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if full.returncode == 0:
            out["revision_full"] = full.stdout.strip() or None
        if short.returncode == 0:
            out["revision_short"] = short.stdout.strip() or None
        if branch.returncode == 0:
            out["branch"] = branch.stdout.strip() or None

        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        dirty: list[str] = []
        if st.returncode == 0 and st.stdout.strip():
            dirty = [
                line.rstrip()
                for line in st.stdout.splitlines()[:_UNCOMMITTED_CAP]
            ]
        out["uncommitted_files"] = dirty
        out["working_tree_clean"] = len(dirty) == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"git_available": False}

    if csv_path is not None:
        p = Path(csv_path).resolve()
        if p.is_file():
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            rel = None
            try:
                rel = p.relative_to(comp).as_posix()
            except ValueError:
                rel = p.name
            out["submission_csv"] = {
                "path": rel,
                "size_bytes": p.stat().st_size,
                "sha256": h.hexdigest(),
            }

    return out


# ---------------------------------------------------------------------------
# ExperimentLog — read, write, render, export
# ---------------------------------------------------------------------------

class ExperimentLog:
    """Manages the .mle_log.jsonl file for a competition directory."""

    def __init__(self, competition_dir: Path):
        self.competition_dir = competition_dir
        self.log_path = competition_dir / LOG_FILENAME

    def create_entry(
        self,
        summary: str,
        author: str | None = None,
        model: str | None = None,
        val_metric: str | None = None,
        val_score: float | None = None,
        submit_score: float | None = None,
        hyperparams: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        working_dir: Path | None = None,
        *,
        event_type: str | None = None,
        linked_checkpoint_id: str | None = None,
        submission_artifact: dict[str, Any] | None = None,
        api_result: dict[str, Any] | None = None,
        csv_path: Path | None = None,
        snapshot_provenance: bool = True,
        git_provenance_extra: dict[str, Any] | None = None,
    ) -> LogEntry:
        """Build a LogEntry with auto-detected fields and optional MLE provenance."""
        wd = working_dir or self.competition_dir
        resolved_author = detect_author(author, wd)
        files_changed, diff_summary, git_hash = get_git_diff_summary(wd)

        vs = None
        if val_score is not None and val_metric:
            vs = {"metric": val_metric, "value": val_score}

        sub_f: float | None = None
        if submit_score is not None:
            try:
                sub_f = float(submit_score)
            except (TypeError, ValueError):
                sub_f = None

        snap: dict[str, Any] = {}
        if snapshot_provenance:
            snap = capture_mle_provenance(
                wd, competition_dir=self.competition_dir, csv_path=csv_path
            )
        sub_art = submission_artifact or snap.get("submission_csv")
        gp = {k: v for k, v in snap.items() if k != "submission_csv"}
        if git_provenance_extra:
            gp = {**gp, **git_provenance_extra}
        git_prov = gp if snapshot_provenance and gp else None

        rev_short = snap.get("revision_short") if snap else None
        if rev_short:
            git_hash = rev_short

        api_clean = sanitize_api_result(api_result) if api_result else None

        et = event_type or "checkpoint"

        return LogEntry(
            id=secrets.token_hex(3),  # 6-char hex
            timestamp=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            author=resolved_author,
            summary=summary,
            model=model,
            val_score=vs,
            submit_score=sub_f,
            hyperparams=hyperparams,
            files_changed=files_changed,
            diff_summary=diff_summary,
            git_hash=git_hash,
            tags=tags or [],
            event_type=et,
            linked_checkpoint_id=linked_checkpoint_id,
            submission_artifact=sub_art,
            git_provenance=git_prov,
            api_result=api_clean,
        )

    def append(self, entry: LogEntry) -> None:
        """Append an entry to the JSONL log."""
        self.competition_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(entry.to_json_line() + "\n")

    def load(self) -> list[LogEntry]:
        """Load all entries from the JSONL log."""
        if not self.log_path.exists():
            return []
        entries = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(LogEntry.from_json_line(line))
        return entries

    # ------------------------------------------------------------------
    # Timeline rendering (ANSI terminal output)
    # ------------------------------------------------------------------

    def render_timeline(self, last_n: int | None = None, expand_all: bool = False) -> str:
        """Render a numbered timeline of experiment entries."""
        entries = self.load()
        if not entries:
            return "No experiment log entries found."

        comp_name = self.competition_dir.name
        total = len(entries)

        if last_n and last_n < total:
            entries = entries[-last_n:]

        lines: list[str] = []
        lines.append("")
        header = f"  MLE Experiment Log: {comp_name}  ({total} entries)"
        lines.append(f"\033[1m{'─' * (len(header) + 4)}\033[0m")
        lines.append(f"\033[1m  {header}  \033[0m")
        lines.append(f"\033[1m{'─' * (len(header) + 4)}\033[0m")
        lines.append("")

        start_idx = total - len(entries)
        for i, entry in enumerate(reversed(entries)):
            num = total - i
            marker = "v" if (i == 0 and expand_all) or expand_all else ">"
            author_tag = "\033[36m[AI]\033[0m" if entry.author == "ai" else "\033[33m[Human]\033[0m"
            ts = entry.timestamp[:16].replace("T", " ")

            line = f"  {marker} #{num:<3} {ts}  {author_tag:18s} {entry.summary}"
            lines.append(line)

            # Detail line
            parts: list[str] = []
            if entry.event_type:
                parts.append(f"event:{entry.event_type}")
            if entry.submit_score is not None:
                parts.append(f"Submit: {entry.submit_score}")
            if entry.val_score:
                parts.append(f"Val: {entry.val_score.get('value', '?')}")
            if entry.model:
                parts.append(f"Model: {entry.model}")
            if entry.diff_summary:
                parts.append(entry.diff_summary)

            if parts:
                lines.append(f"    {'  |  '.join(parts)}")

            if expand_all:
                if entry.files_changed:
                    files_str = ", ".join(entry.files_changed)
                    lines.append(f"    Files: {files_str}")
                if entry.hyperparams:
                    lines.append(f"    Hyperparams: {json.dumps(entry.hyperparams)}")
                if entry.tags:
                    lines.append(f"    Tags: {', '.join(entry.tags)}")
                if entry.git_hash:
                    lines.append(f"    Git: {entry.git_hash}")
                if entry.git_provenance:
                    gfp = entry.git_provenance
                    if gfp.get("revision_full"):
                        lines.append(f"    Rev: {gfp['revision_full']}")
                    if gfp.get("branch"):
                        lines.append(f"    Branch: {gfp['branch']}")
                if entry.submission_artifact:
                    lines.append(f"    CSV: {entry.submission_artifact}")
                if entry.api_result:
                    ar = json.dumps(entry.api_result, ensure_ascii=False)
                    lines.append(f"    API: {ar[:200]}{'…' if len(ar) > 200 else ''}")

            lines.append("")

        lines.append("  Legend: v = expanded  > = collapsed  \033[36m[AI]\033[0m = agent  \033[33m[Human]\033[0m = you")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Export (CSV and Markdown)
    # ------------------------------------------------------------------

    def export_csv(self) -> str:
        """Export log as CSV string."""
        entries = self.load()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["#", "Timestamp", "Author", "Summary", "Model",
                         "Val Score", "Submit Score", "Tags"])
        for i, e in enumerate(entries, 1):
            val = e.val_score.get("value", "") if e.val_score else ""
            writer.writerow([
                i, e.timestamp, e.author, e.summary,
                e.model or "", val,
                e.submit_score if e.submit_score is not None else "",
                ", ".join(e.tags),
            ])
        return buf.getvalue()

    def export_markdown(self) -> str:
        """Export log as Markdown table string."""
        entries = self.load()
        lines = [
            "| # | Timestamp | Author | Summary | Model | Val Score | Submit Score | Tags |",
            "|---|-----------|--------|---------|-------|-----------|-------------|------|",
        ]
        for i, e in enumerate(entries, 1):
            val = e.val_score.get("value", "") if e.val_score else ""
            sub = e.submit_score if e.submit_score is not None else ""
            lines.append(
                f"| {i} | {e.timestamp} | {e.author} | {e.summary} | "
                f"{e.model or ''} | {val} | {sub} | {', '.join(e.tags)} |"
            )
        return "\n".join(lines) + "\n"
