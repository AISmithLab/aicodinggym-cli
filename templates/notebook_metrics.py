#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path

FLOAT_RE = r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?"


def execute_notebook(notebook_path: Path) -> str:
    cmd = [
        "jupyter",
        "nbconvert",
        "--to",
        "notebook",
        "--execute",
        str(notebook_path),
        "--inplace",
    ]
    run = subprocess.run(cmd, capture_output=True, text=True)
    combined = (run.stdout or "") + "\n" + (run.stderr or "")
    print(combined.strip())
    if run.returncode != 0:
        raise RuntimeError("Notebook execution failed")
    return combined


def extract_values_from_text(text: str) -> list[float]:
    patterns = [
        rf"validation_accuracy[^0-9-+]*({FLOAT_RE})",
        rf"VAL_ACC:\s*({FLOAT_RE})",
    ]
    values: list[float] = []
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            try:
                values.append(float(match))
            except ValueError:
                continue
    return values


def extract_values_from_notebook(notebook_path: Path) -> list[float]:
    data = json.loads(notebook_path.read_text(encoding="utf-8"))
    values: list[float] = []
    for cell in data.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        for output in cell.get("outputs", []):
            chunks: list[str] = []
            if "text" in output:
                text = output["text"]
                chunks.extend(text if isinstance(text, list) else [text])
            if "data" in output:
                for key in ("text/plain", "text/markdown"):
                    if key in output["data"]:
                        val = output["data"][key]
                        chunks.extend(val if isinstance(val, list) else [val])
            for chunk in chunks:
                values.extend(extract_values_from_text(str(chunk)))
    return values


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: notebook_metrics.py <path-to-notebook>", file=sys.stderr)
        return 2

    notebook_path = Path(sys.argv[1]).resolve()
    if not notebook_path.exists():
        print(f"Notebook not found: {notebook_path}", file=sys.stderr)
        return 2

    try:
        stdout_text = execute_notebook(notebook_path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    values = extract_values_from_text(stdout_text)
    values.extend(extract_values_from_notebook(notebook_path))
    if not values:
        print("MAX_VALIDATION_ACCURACY=NA")
        return 0

    print(f"MAX_VALIDATION_ACCURACY={max(values):.10g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
