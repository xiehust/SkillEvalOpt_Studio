"""Read-only parsing of job output artifacts into frontend-renderable JSON.

Every filesystem access resolves the requested path and requires it to stay
inside the job's ``out/`` root — traversal (``../``, absolute paths) raises
ValueError, which the API maps to 400.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from skillopt_studio.config import StudioConfig
from skillopt_studio.models import JobInfo

MAX_TEXT_ARTIFACT_BYTES = 512 * 1024

_STEP_FIELDS = (
    "step", "epoch", "action", "selection_hard", "selection_soft",
    "current_score", "best_score", "best_step", "skill_len", "wall_time_s",
)

_ROW_FIELDS = ("id", "task_type", "hard", "soft", "judge_reason", "duration_s", "error", "judge_error")


def job_out_root(config: StudioConfig, job: JobInfo) -> Path:
    if job.out_root:
        return Path(job.out_root)
    return config.jobs_dir / job.id / "out"


def _safe_resolve(out_root: Path, rel_path: str) -> Path:
    """Resolve rel_path strictly inside out_root; ValueError on escape."""
    rel = str(rel_path or "").strip()
    if rel.startswith(("/", "\\")) or rel.startswith("~"):
        raise ValueError(f"artifact path must be relative, got {rel!r}")
    root = out_root.resolve()
    candidate = (root / rel).resolve() if rel else root
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"artifact path {rel!r} escapes the job output directory")
    return candidate


def safe_target(config: StudioConfig, job: JobInfo, rel_path: str = "") -> Path:
    """Public guard for API callers: resolved path strictly inside out/."""
    return _safe_resolve(job_out_root(config, job), rel_path)


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def eval_results(config: StudioConfig, job: JobInfo) -> dict | None:
    """Summary + per-task rows from out/results.json (None until it exists)."""
    out = job_out_root(config, job)
    results = _read_json(out / "results.json")
    if not isinstance(results, list):
        return None
    rows = [{key: r.get(key) for key in _ROW_FIELDS if key in r} for r in results if isinstance(r, dict)]
    return {
        "summary": {
            "tasks": len(rows),
            "pass_rate": round(_mean([float(r.get("hard") or 0) for r in rows]), 4),
            "soft_mean": round(_mean([float(r.get("soft") or 0.0) for r in rows]), 4),
            "duration_s": round(sum(float(r.get("duration_s") or 0.0) for r in rows), 1),
        },
        "rows": rows,
    }


def train_summary(config: StudioConfig, job: JobInfo) -> dict | None:
    """Step timeline + final summary from history.json / summary.json.

    Works mid-run: history.json grows step by step, summary.json appears at
    the end.  Returns None only when neither exists yet.
    """
    out = job_out_root(config, job)
    history = _read_json(out / "history.json")
    summary = _read_json(out / "summary.json")
    if not isinstance(history, list):
        history = []
    if not isinstance(summary, dict):
        summary = {}
    if not history and not summary:
        return None

    steps = [
        {key: rec.get(key) for key in _STEP_FIELDS}
        for rec in history
        if isinstance(rec, dict)
    ]
    best_step = summary.get("best_step")
    if best_step is None and steps:
        best_step = steps[-1].get("best_step")

    token_summary = summary.get("token_summary") or {}
    token_totals = token_summary.get("_total") or {}
    return {
        "steps": steps,
        "best_step": best_step,
        "best_score": summary.get("best_selection_hard", (steps[-1].get("best_score") if steps else None)),
        "baseline_selection_hard": summary.get("baseline_selection_hard"),
        "test_scores": {
            "baseline": summary.get("baseline_test_hard"),
            "best": summary.get("test_hard"),
            "final": summary.get("final_test_hard"),
        },
        "totals": {
            "steps": summary.get("total_steps", len(steps)),
            "accepts": summary.get("total_accepts"),
            "rejects": summary.get("total_rejects"),
            "skips": summary.get("total_skips"),
            "wall_time_s": summary.get("total_wall_time_s"),
        },
        "token_totals": token_totals,
        "finished": bool(summary),
    }


def skill_diff(config: StudioConfig, job: JobInfo) -> str:
    """Unified diff between the seed skill (skills/skill_v0000.md) and best_skill.md."""
    out = job_out_root(config, job)
    seed_path = out / "skills" / "skill_v0000.md"
    best_path = out / "best_skill.md"
    if not seed_path.is_file() or not best_path.is_file():
        return ""
    seed = seed_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    best = best_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(seed, best, fromfile="skills/skill_v0000.md", tofile="best_skill.md")
    )


_PROGRESS_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\[STEP (\d+) done\]"), "step {0} done"),
    (re.compile(r"STEP (\d+)\b"), "step {0} running"),
    (re.compile(r"\[skilleval\] judging (\d+)"), "judging {0} responses"),
    (re.compile(r"\[skilleval\] tasks: (\d+)"), "rollout {0} tasks"),
    (re.compile(r"\[studio\] step (\d)/2: bundle build"), "bundle build"),
    (re.compile(r"\[studio\] step (\d)/2: train"), "train starting"),
    (re.compile(r"(rollout)", re.IGNORECASE), "rollout in progress"),
)


def job_progress(config: StudioConfig, job: JobInfo) -> str:
    """Short progress phrase from the log tail + artifact existence."""
    if job.status in ("succeeded", "failed", "cancelled"):
        return job.status
    if job.status == "queued":
        return "queued"
    log_path = config.jobs_dir / job.id / "log.txt"
    if log_path.is_file():
        try:
            with open(log_path, "rb") as f:
                f.seek(max(0, log_path.stat().st_size - 8192))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            tail = ""
        for line in reversed(tail.splitlines()):
            for pattern, template in _PROGRESS_PATTERNS:
                match = pattern.search(line)
                if match:
                    return template.format(*match.groups())
    out = job_out_root(config, job)
    if (out / "results.json").is_file() or (out / "history.json").is_file():
        return "finalizing"
    return "running"


def list_artifacts(config: StudioConfig, job: JobInfo, rel_path: str = "") -> dict:
    """Directory listing under out/ — {path, dirs:[names], files:[{name,size}]}."""
    out = job_out_root(config, job)
    target = _safe_resolve(out, rel_path)
    if not target.is_dir():
        raise FileNotFoundError(rel_path or ".")
    dirs, files = [], []
    for entry in sorted(target.iterdir()):
        if entry.is_dir():
            dirs.append(entry.name)
        else:
            files.append({"name": entry.name, "size": entry.stat().st_size})
    relative = "" if target == out.resolve() else str(target.relative_to(out.resolve()))
    return {"path": relative, "dirs": dirs, "files": files}


def read_artifact(config: StudioConfig, job: JobInfo, rel_path: str) -> dict:
    """Text file content (binary files return metadata only, never bytes)."""
    out = job_out_root(config, job)
    target = _safe_resolve(out, rel_path)
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    size = target.stat().st_size
    with open(target, "rb") as f:
        data = f.read(MAX_TEXT_ARTIFACT_BYTES + 1)
    if b"\x00" in data:
        return {"path": rel_path, "kind": "binary", "size": size}
    try:
        text = data[:MAX_TEXT_ARTIFACT_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        return {"path": rel_path, "kind": "binary", "size": size}
    return {
        "path": rel_path,
        "kind": "text",
        "size": size,
        "truncated": size > MAX_TEXT_ARTIFACT_BYTES,
        "content": text,
    }
