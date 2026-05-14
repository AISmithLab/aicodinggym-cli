#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from pathlib import Path

FLOAT_RE = r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?"


def execute_notebook(notebook_path: Path) -> str:
    """Run nbconvert --execute; try ``jupyter`` then ``python -m jupyter`` (Windows)."""
    nb = str(notebook_path)
    startup_timeout = int(os.environ.get("AICODINGGYM_NB_STARTUP_TIMEOUT_SEC", "240"))
    exec_timeout = int(os.environ.get("AICODINGGYM_NB_EXEC_TIMEOUT_SEC", "0"))
    common = [
        "nbconvert",
        "--to",
        "notebook",
        "--execute",
        nb,
        "--inplace",
        f"--ExecutePreprocessor.startup_timeout={startup_timeout}",
        f"--ExecutePreprocessor.timeout={exec_timeout}",
    ]
    variants = (
        ["jupyter", *common],
        [sys.executable, "-m", "jupyter", *common],
    )
    last_combined = ""
    for cmd in variants:
        run = subprocess.run(cmd, capture_output=True, text=True)
        last_combined = (run.stdout or "") + "\n" + (run.stderr or "")
        if run.returncode == 0:
            print(last_combined.strip())
            return last_combined
    raise RuntimeError(last_combined.strip() or "Notebook execution failed (jupyter nbconvert)")


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


MODEL_PATTERNS: list[tuple[str, str]] = [
    (r"LGBMClassifier|LGBMRegressor|lgb\.train|lightgbm\.train", "LightGBM"),
    (r"XGBClassifier|XGBRegressor|xgb\.train", "XGBoost"),
    (r"CatBoostClassifier|CatBoostRegressor", "CatBoost"),
    (r"RandomForestClassifier|RandomForestRegressor", "RandomForest"),
    (r"HistGradientBoostingClassifier|HistGradientBoostingRegressor", "HistGradientBoosting"),
    (r"LogisticRegression", "LogisticRegression"),
    (r"GradientBoostingClassifier|GradientBoostingRegressor", "GradientBoosting"),
    (r"SVC\b|SVR\b", "SVM"),
    (r"MLPClassifier|MLPRegressor", "MLP"),
    (r"KNeighborsClassifier|KNeighborsRegressor", "KNN"),
    (r"ExtraTreesClassifier|ExtraTreesRegressor", "ExtraTrees"),
    (r"DecisionTreeClassifier|DecisionTreeRegressor", "DecisionTree"),
]

HYPERPARAM_PATTERNS: list[tuple[str, str]] = [
    (r"n_estimators\s*=\s*(\d+)", "n_estimators"),
    (r"learning_rate\s*=\s*([\d.eE+-]+)", "learning_rate"),
    (r"max_depth\s*=\s*([\d-]+)", "max_depth"),
    (r"num_leaves\s*=\s*(\d+)", "num_leaves"),
    (r"subsample\s*=\s*([\d.]+)", "subsample"),
    (r"colsample_bytree\s*=\s*([\d.]+)", "colsample_bytree"),
    (r"min_child_samples\s*=\s*(\d+)", "min_child_samples"),
    (r"reg_alpha\s*=\s*([\d.eE+-]+)", "reg_alpha"),
    (r"reg_lambda\s*=\s*([\d.eE+-]+)", "reg_lambda"),
    (r"C\s*=\s*([\d.eE+-]+)", "C"),
    (r"n_neighbors\s*=\s*(\d+)", "n_neighbors"),
    (r"max_iter\s*=\s*(\d+)", "max_iter"),
]


def extract_model_info(notebook_path: Path) -> tuple[str, str]:
    """Detect primary ML model and key hyperparameters from notebook source cells."""
    try:
        data = json.loads(notebook_path.read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    parts: list[str] = []
    for cell in data.get("cells", []):
        if cell.get("cell_type") == "code":
            src = cell.get("source", [])
            if isinstance(src, list):
                src = "".join(src)
            parts.append(src)
    all_source = "\n".join(parts)

    model_name = ""
    for pattern, name in MODEL_PATTERNS:
        if re.search(pattern, all_source, re.IGNORECASE):
            model_name = name
            break

    params: list[str] = []
    for pattern, label in HYPERPARAM_PATTERNS:
        m = re.search(pattern, all_source, re.IGNORECASE)
        if m:
            params.append(f"{label}={m.group(1)}")

    return model_name, ", ".join(params[:8])


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: notebook_metrics.py <path-to-notebook>", file=sys.stderr)
        return 2

    notebook_path = Path(sys.argv[1]).resolve()
    if not notebook_path.exists():
        print(f"Notebook not found: {notebook_path}", file=sys.stderr)
        return 2

    combined = ""
    exec_ok = False
    try:
        combined = execute_notebook(notebook_path)
        exec_ok = True
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)

    values = extract_values_from_text(combined)
    values.extend(extract_values_from_notebook(notebook_path))
    if not values:
        print("MAX_VALIDATION_ACCURACY=NA")
        return 0 if exec_ok else 1

    print(f"MAX_VALIDATION_ACCURACY={max(values):.10g}")
    model_name, hyperparams = extract_model_info(notebook_path)
    if model_name:
        print(f"MODEL_NAME={model_name}")
    if hyperparams:
        print(f"HYPERPARAMS={hyperparams}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
