import re
from datetime import datetime
from pathlib import Path

SESSION_LOG_DIR = ".log"
SESSION_LOG_HEADER = """# Session Log

**Problem:** {problem}
**Challenge type:** {challenge_type}
**Started:** {started}
**Agent:** {agent}

---
"""


def next_session_entry_num(log_path: Path) -> int:
    """Next ``## Entry N`` index from an existing session log file."""
    if not log_path.exists():
        return 1
    text = log_path.read_text(encoding="utf-8")
    nums = [
        int(m.group(1))
        for m in re.finditer(r"^## Entry (\d+)\s*$", text, re.MULTILINE)
    ]
    return max(nums) + 1 if nums else 1


def mle_summary_footer_markdown(competition_id: str) -> str:
    """Standard footer pointing humans at structured logs (AGENTS.md contract)."""
    return f"""
**Where to view summaries (structured metrics & history):**
- Tabular timeline (CV vs leaderboard, deltas): `{competition_id}/gym_log.json` — in Python: `from aicodinggym.gym_logger import print_summary, set_log_path` then `set_log_path("{competition_id}/gym_log.json"); print_summary()` (or open the JSON).
- Checkpoints, git hash, author: `{competition_id}/.mle_log.jsonl` — terminal: `aicodinggym mle log show {competition_id}` (or `aicodinggym mle log export {competition_id}`).
- This narrative thread: `{competition_id}/.log/` (this file).
"""


class SessionLogger:
    def __init__(self, problem_dir: Path, agent: str, challenge_type: str):
        self.problem_dir = Path(problem_dir)
        self.agent = agent
        self.challenge_type = challenge_type
        self.log_dir = self.problem_dir / SESSION_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self._get_log_path()
        if not self.log_path.exists():
            self._write_header()

    def _get_log_path(self) -> Path:
        """Pick a log file to append to.

        Prefer ``<agent>-*.md`` for this agent (most recently modified).
        If none exist, reuse the **newest** ``*.md`` in ``.log/`` so CLI runs
        still append after ``AICODINGGYM_AGENT`` / default label changes
        (e.g. GitHub Copilot vs AI assistant).
        Otherwise create ``<agent>-YYYYMMDD-HHMMSS.md``.
        """
        now = datetime.now().strftime("%Y%m%d-%H%M%S")
        agent_key = self.agent.lower().replace(" ", "-")
        pattern = f"{agent_key}-*.md"
        agent_matches = list(self.log_dir.glob(pattern))
        if agent_matches:
            return max(agent_matches, key=lambda p: p.stat().st_mtime)
        any_logs = list(self.log_dir.glob("*.md"))
        if any_logs:
            return max(any_logs, key=lambda p: p.stat().st_mtime)
        return self.log_dir / f"{agent_key}-{now}.md"

    def _write_header(self):
        header = SESSION_LOG_HEADER.format(
            problem=self.problem_dir.name,
            challenge_type=self.challenge_type,
            started=datetime.now().isoformat(timespec="seconds"),
            agent=self.agent,
        )
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(header)

    def append_entry(
        self,
        user_prompt: str,
        approach: str,
        files_touched: list[str],
        outcome: str,
        *,
        entry_num: int | None = None,
        entry_kind: str | None = None,
        cli_submit_message: str | None = None,
        extra_sections: str | None = None,
        include_summary_footer: bool = False,
    ) -> int:
        """Append one session log entry. Returns the entry number used.

        If ``entry_num`` is None, the next number is derived from existing
        ``## Entry`` headings in this file (avoids collisions with chat entries).
        """
        num = entry_num if entry_num is not None else next_session_entry_num(self.log_path)
        submit_line = ""
        if cli_submit_message:
            submit_line = f"\n**CLI submit message (`-m`):** {cli_submit_message}\n"
        kind_line = ""
        if entry_kind:
            kind_line = f"\n**Entry kind:** {entry_kind}\n"
        extra = f"\n{extra_sections}\n" if extra_sections else ""
        footer = ""
        if include_summary_footer:
            footer = mle_summary_footer_markdown(self.problem_dir.name)
        entry = f"""
## Entry {num}

**Time:** {datetime.now().isoformat(timespec='seconds')}
**User prompt:** {user_prompt}{submit_line}{kind_line}
**Approach:** {approach}
**Files touched:** {', '.join(files_touched) if files_touched else 'None'}
**Outcome:** {outcome}
{extra}{footer}"""
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(entry)
        return num
