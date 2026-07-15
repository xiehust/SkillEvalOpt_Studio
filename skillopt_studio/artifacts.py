"""Read-only parsing of job output artifacts into frontend-renderable JSON.

Every filesystem access resolves the requested path and requires it to stay
inside the job's ``out/`` root — traversal (``../``, absolute paths) raises
ValueError, which the API maps to 400.
"""
from __future__ import annotations

import difflib
import json
import re
from collections import OrderedDict
from pathlib import Path

from skillopt_studio.config import StudioConfig
from skillopt_studio.models import JobInfo

MAX_TEXT_ARTIFACT_BYTES = 512 * 1024

_STEP_FIELDS = (
    "step", "epoch", "action", "selection_hard", "selection_soft",
    "current_score", "best_score", "best_step", "skill_len", "wall_time_s",
    "selected_skills", "attribution_counts", "current_skill_scores",
    "regressions", "gate_reasons", "excluded_failures",
)

_ROW_FIELDS = (
    "id", "task_type", "hard", "soft", "judge_reason", "duration_s", "error", "judge_error",
    "target_skills", "usage", "judge_usage",
)

_USAGE_KEYS = ("input", "cache_write", "cache_read", "output", "total")

_FINISHED_STATUSES = {"succeeded", "failed", "cancelled"}


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


def _sum_usage(rows: list[dict]) -> dict | None:
    """Sum exec ``usage`` + judge ``judge_usage`` over result rows.

    judge_usage carries only input/output; they are folded into the same
    four-way breakdown. Returns None when no row has any usage data.
    """
    totals = dict.fromkeys(_USAGE_KEYS, 0)
    found = False
    for row in rows:
        usage = row.get("usage")
        if isinstance(usage, dict):
            found = True
            for key in _USAGE_KEYS:
                totals[key] += int(usage.get(key) or 0)
        judge_usage = row.get("judge_usage")
        if isinstance(judge_usage, dict):
            found = True
            input_t = int(judge_usage.get("input") or 0)
            output_t = int(judge_usage.get("output") or 0)
            totals["input"] += input_t
            totals["output"] += output_t
            totals["total"] += input_t + output_t
    return totals if found else None


def eval_results(config: StudioConfig, job: JobInfo) -> dict | None:
    """Summary + per-task rows from out/results.json (None until it exists)."""
    out = job_out_root(config, job)
    results = _read_json(out / "results.json")
    if not isinstance(results, list):
        return None
    rows = [{key: r.get(key) for key in _ROW_FIELDS if key in r} for r in results if isinstance(r, dict)]
    aggregates = _read_json(out / "summary.json")
    if not isinstance(aggregates, dict) or not isinstance(aggregates.get("overall"), dict):
        aggregates = None
    return {
        "summary": {
            "tasks": len(rows),
            "pass_rate": round(_mean([float(r.get("hard") or 0) for r in rows]), 4),
            "soft_mean": round(_mean([float(r.get("soft") or 0.0) for r in rows]), 4),
            "duration_s": round(sum(float(r.get("duration_s") or 0.0) for r in rows), 1),
            "tokens": _sum_usage(rows),
        },
        "aggregates": aggregates,
        "rows": rows,
    }


def taskgen_results(config: StudioConfig, job: JobInfo) -> dict | None:
    """Generated tasks + summary from out/generated_tasks.json (None until it exists)."""
    out = job_out_root(config, job)
    tasks = _read_json(out / "generated_tasks.json")
    if not isinstance(tasks, list):
        return None
    summary = _read_json(out / "gen_summary.json")
    return {
        "tasks": [t for t in tasks if isinstance(t, dict)],
        "summary": summary if isinstance(summary, dict) else {},
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

    mode = (
        "plugin"
        if summary.get("mode") == "plugin"
        or job.params.get("target_mode") == "plugin"
        or job.params.get("skill_ids") is not None
        else "skill"
    )
    steps = []
    for rec in history:
        if not isinstance(rec, dict):
            continue
        row = {key: rec.get(key) for key in _STEP_FIELDS}
        if mode == "plugin":
            aggregates = rec.get("candidate_aggregates") or rec.get("train_aggregates") or {}
            overall = aggregates.get("overall") if isinstance(aggregates, dict) else {}
            if isinstance(overall, dict):
                row["selection_hard"] = overall.get("hard")
                row["selection_soft"] = overall.get("soft")
        steps.append(row)
    best_step = summary.get("best_step")
    if best_step is None and steps:
        best_step = steps[-1].get("best_step")

    token_summary = summary.get("token_summary") or {}
    token_totals = token_summary.get("_total") or {}
    excluded_failure_count = sum(
        len(failures)
        for step in steps
        if isinstance((failures := step.get("excluded_failures")), list)
    )
    result = {
        "mode": mode,
        "steps": steps,
        "best_step": best_step,
        "best_score": summary.get(
            "best_selection_score",
            summary.get("best_selection_hard", (steps[-1].get("best_score") if steps else None)),
        ),
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
            "excluded_failures": excluded_failure_count,
        },
        "token_totals": token_totals,
        "finished": bool(summary),
    }
    if mode == "plugin":
        baseline = summary.get("baseline_aggregates")
        best = summary.get("best_aggregates")
        test = summary.get("test_aggregates")
        baseline_overall = baseline.get("overall") if isinstance(baseline, dict) else None
        test_overall = test.get("overall") if isinstance(test, dict) else None
        result["baseline_selection_hard"] = (
            baseline_overall.get("hard") if isinstance(baseline_overall, dict) else None
        )
        result["test_scores"] = {
            "baseline": None,
            "best": test_overall.get("hard") if isinstance(test_overall, dict) else None,
            "final": test_overall.get("hard") if isinstance(test_overall, dict) else None,
        }
        result["plugin"] = {
            "skill_names": summary.get("skill_names") or [],
            "trainable_skill_names": summary.get("trainable_skill_names") or [],
            "baseline": baseline,
            "best": best,
            "test": test,
            "best_skill_scores": summary.get("best_skill_scores") or {},
        }
    return result


# Finished jobs' artifacts are immutable, so token aggregation is cached by
# job id (bounded; oldest evicted). Running jobs are never cached.
_TOKENS_CACHE: OrderedDict[str, dict | None] = OrderedDict()
_TOKENS_CACHE_MAX = 256


def _sum_exec_raw_usage(root: Path) -> dict | None:
    """Sum exec usage over every persisted raw transcript under *root*.

    Train jobs scatter rollouts across step/eval subdirectories, so this walks
    recursively; taskgen has a single raw at the top level. Returns None when
    no raw carries usage (e.g. pre-json-mode artifacts).
    """
    try:
        from skillopt.model.codex_harness import extract_exec_usage
    except ImportError:
        return None
    totals = dict.fromkeys(_USAGE_KEYS, 0)
    found = False
    for raw_name in ("claude_raw.txt", "codex_raw.txt"):
        for path in sorted(root.rglob(raw_name)):
            try:
                usage = extract_exec_usage(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if usage:
                found = True
                for key in _USAGE_KEYS:
                    totals[key] += int(usage.get(key) or 0)
    return totals if found else None


def _job_tokens_uncached(config: StudioConfig, job: JobInfo) -> dict | None:
    out = job_out_root(config, job)
    if job.type == "eval":
        results = _read_json(out / "results.json")
        if not isinstance(results, list):
            return None
        return _sum_usage([r for r in results if isinstance(r, dict)])
    if job.type == "taskgen":
        # taskgen has no per-task usage; extract from the persisted exec raws.
        return _sum_exec_raw_usage(out)
    if job.type == "train":
        # optimizer-side chat totals from summary.json …
        summary = _read_json(out / "summary.json")
        total = (summary.get("token_summary") or {}).get("_total") if isinstance(summary, dict) else None
        optimizer = None
        if total:
            optimizer = {
                "input": int(total.get("prompt_tokens") or 0),
                "cache_write": 0,
                "cache_read": 0,
                "output": int(total.get("completion_tokens") or 0),
                "total": int(total.get("total_tokens") or 0),
            }
        # … plus target exec rollouts persisted under steps/ and eval dirs
        rollout = _sum_exec_raw_usage(out)
        if optimizer is None and rollout is None:
            return None
        combined = dict.fromkeys(_USAGE_KEYS, 0)
        for part in (optimizer, rollout):
            if part:
                for key in _USAGE_KEYS:
                    combined[key] += part[key]
        return combined
    return None


def job_tokens(config: StudioConfig, job: JobInfo) -> dict | None:
    """Job-level token totals (four-way breakdown + total), None when unknown.

    eval — sums exec usage + judge usage over results.json rows.
    taskgen — extracts from persisted exec raw transcripts.
    train — summary.json token_summary._total (optimizer-side chat) plus the
    target exec rollout raws persisted under steps/ and eval directories.
    Running/unknown-type jobs return None; presentation must never fail the API.
    """
    if job.status not in _FINISHED_STATUSES:
        return None
    if job.id in _TOKENS_CACHE:
        _TOKENS_CACHE.move_to_end(job.id)
        return _TOKENS_CACHE[job.id]
    try:
        tokens = _job_tokens_uncached(config, job)
    except Exception:  # noqa: BLE001 — display feature; jobs API must stay up
        tokens = None
    _TOKENS_CACHE[job.id] = tokens
    if len(_TOKENS_CACHE) > _TOKENS_CACHE_MAX:
        _TOKENS_CACHE.popitem(last=False)
    return tokens


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


def plugin_skill_diffs(config: StudioConfig, job: JobInfo) -> dict[str, str]:
    """Per-Skill diffs between Plugin version zero and best_plugin."""
    out = job_out_root(config, job)
    initial = out / "plugin_versions" / "plugin_v0000"
    best = out / "best_plugin"
    manifest = _read_json(best / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("skill_names"), list):
        return {}
    diffs: dict[str, str] = {}
    for raw_name in manifest["skill_names"]:
        if not isinstance(raw_name, str) or not raw_name:
            continue
        seed_path = initial / raw_name / "SKILL.md"
        best_path = best / raw_name / "SKILL.md"
        if not seed_path.is_file() or not best_path.is_file():
            continue
        seed_lines = seed_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines(keepends=True)
        best_lines = best_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(
                seed_lines,
                best_lines,
                fromfile=f"plugin_v0000/{raw_name}/SKILL.md",
                tofile=f"best_plugin/{raw_name}/SKILL.md",
            )
        )
        if diff:
            diffs[raw_name] = diff
    return diffs


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
