#!/usr/bin/env python3
"""SkillOpt skilleval: evaluate an arbitrary skill on a custom task set.

Runs a user-provided SKILL.md inside Claude Code CLI on each task of a
user-provided task file (JSON array / JSONL with id/question/rubric), scores
every response with an LLM judge against the task's rubric, and writes
``results.json`` + ``report.md``.

Usage
-----
    python3 scripts/evaluate_skill.py \
        --skill ~/.claude/skills/my-skill/SKILL.md \
        --tasks data/my_tasks.json \
        --out_root outputs/skilleval_myskill

Backend configuration follows the same environment conventions as train.py /
eval_only.py (AZURE_OPENAI_*, ANTHROPIC_*, etc.); the target backend defaults
to ``claude_code_exec`` and the judge uses the optimizer backend.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from skillopt.envs.skilleval.dataloader import load_tasks
from skillopt.envs.skilleval.evaluator import judge
from skillopt.envs.skilleval.rollout import run_batch
from skillopt.model import (
    configure_claude_code_exec,
    set_optimizer_backend,
    set_optimizer_deployment,
    set_target_backend,
    set_target_deployment,
)
from skillopt.model.common import default_model_for_backend


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SkillOpt skilleval — evaluate a custom skill")
    p.add_argument("--skill", type=str, required=True,
                   help="Skill to evaluate: a markdown file, or a skill directory "
                        "containing SKILL.md (supporting files are copied along)")
    p.add_argument("--tasks", type=str, required=True,
                   help="Task file (JSON array or JSONL; id/question/rubric per item)")
    p.add_argument("--out_root", type=str, required=True,
                   help="Output directory for results.json / report.md / rollouts/")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--timeout", type=int, default=600,
                   help="Per-task Claude Code timeout in seconds")
    p.add_argument("--limit", type=int, default=0,
                   help="Only run the first N tasks (0 = all)")
    p.add_argument("--model", type=str, default="",
                   help="Target model override for claude_code_exec")
    p.add_argument("--target_backend", type=str, default="claude_code_exec")
    p.add_argument("--optimizer_backend", type=str, default="openai_chat",
                   help="Judge backend")
    p.add_argument("--optimizer_model", type=str, default="",
                   help="Judge model override")
    p.add_argument("--claude_code_exec_path", type=str, default="claude")
    p.add_argument("--claude_code_exec_effort", type=str, default="medium")
    return p.parse_args()


_SKIP_DIRS = {"__pycache__", "node_modules", ".git"}


def _collect_skill(path: str) -> tuple[str, list[tuple[str, str]]]:
    """Resolve --skill into (SKILL.md content, supporting files).

    A file path is single-file mode (no supporting files). A directory must
    contain SKILL.md; every other regular file in it is returned as an
    ``(absolute src, path relative to the skill dir)`` pair, skipping hidden
    entries and tooling caches. Symlinks are not followed (an evaluation
    workspace must never be able to reach back into the source skill).
    """
    if os.path.isdir(path):
        skill_md_path = os.path.join(path, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            sys.exit(f"error: skill directory has no SKILL.md: {path}")
        content = _read_skill_file(skill_md_path)
        support: list[tuple[str, str]] = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d not in _SKIP_DIRS]
            for name in sorted(files):
                if name.startswith("."):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, path)
                if rel == "SKILL.md" or os.path.islink(full):
                    continue
                support.append((os.path.abspath(full), rel))
        return content, support
    return _read_skill_file(path), []


def _read_skill_file(path: str) -> str:
    if not os.path.isfile(path):
        sys.exit(f"error: skill file not found: {path}")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        sys.exit(f"error: skill file is empty: {path}")
    return content


def _artifacts_listing(work_dir: str) -> str:
    """List files the agent left in work_dir (relative path + size), skipping harness files."""
    skip_prefixes = (".agents", ".claude")
    lines = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, work_dir)
            if rel.startswith(skip_prefixes) or rel == "task.md":
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            lines.append(f"{rel} ({size} bytes)")
    return "\n".join(lines)


def merge_scores(items: list[dict], rollout_results: list[dict], judge_fn) -> list[dict]:
    """Merge rollout results with judge verdicts; errored tasks skip the judge."""
    merged = []
    for item, rollout_result in zip(items, rollout_results):
        result = dict(rollout_result)
        if result.get("error"):
            result.update({"hard": 0, "soft": 0.0, "judge_reason": ""})
        else:
            listing = _artifacts_listing(result["work_dir"]) if os.path.isdir(
                result.get("work_dir", "")
            ) else ""
            verdict = judge_fn(item, result.get("response", ""), listing)
            result.update(verdict)
        merged.append(result)
    return merged


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_report(results: list[dict]) -> str:
    """Render the human-readable evaluation report (pure function)."""
    total = len(results)
    pass_rate = _mean([float(r.get("hard", 0)) for r in results])
    soft_mean = _mean([float(r.get("soft", 0.0)) for r in results])
    total_duration = sum(float(r.get("duration_s", 0.0)) for r in results)

    lines = [
        "# Skill Evaluation Report",
        "",
        "## Summary",
        "",
        f"- Tasks: {total}",
        f"- Pass rate (hard): {pass_rate:.1%}",
        f"- Soft score mean: {soft_mean:.3f}",
        "",
    ]

    # Per-task_type breakdown
    by_type: dict[str, list[dict]] = {}
    for r in results:
        by_type.setdefault(str(r.get("task_type", "default")), []).append(r)
    if by_type:
        lines += ["## By task type", "",
                  "| task_type | tasks | pass rate | soft mean |",
                  "|---|---|---|---|"]
        for task_type in sorted(by_type):
            group = by_type[task_type]
            lines.append(
                f"| {task_type} | {len(group)} "
                f"| {_mean([float(r.get('hard', 0)) for r in group]):.1%} "
                f"| {_mean([float(r.get('soft', 0.0)) for r in group]):.3f} |"
            )
        lines.append("")

    # Per-task detail
    lines += ["## Tasks", "",
              "| id | pass | soft | judge reason | duration (s) |",
              "|---|---|---|---|---|"]
    for r in results:
        reason = str(r.get("judge_reason", "")).replace("|", "\\|").replace("\n", " ")
        if len(reason) > 80:
            reason = reason[:77] + "..."
        mark = "✓" if r.get("hard") else "✗"
        lines.append(
            f"| {r.get('id')} | {mark} | {float(r.get('soft', 0.0)):.2f} "
            f"| {reason} | {float(r.get('duration_s', 0.0)):.1f} |"
        )
    lines.append("")

    # Cost
    lines += ["## Cost", "",
              f"- Total duration: {total_duration:.1f}s",
              f"- Mean duration per task: {_mean([float(r.get('duration_s', 0.0)) for r in results]):.1f}s",
              "- Token usage: n/a (not parsed in minimal version)",
              ""]

    # Failures
    errored = [r for r in results if r.get("error")]
    judge_errored = [r for r in results if r.get("judge_error")]
    lines += ["## Failures", ""]
    if not errored and not judge_errored:
        lines.append("none")
    if errored:
        lines.append("### Rollout errors (scored 0, judge skipped)")
        lines += [f"- `{r['id']}`: {r['error']}" for r in errored]
    if judge_errored:
        lines.append("### Judge errors (scored 0, verdict unavailable)")
        lines += [f"- `{r['id']}`: {r['judge_error']}" for r in judge_errored]
    lines.append("")

    return "\n".join(lines)


def _configure_backends(args: argparse.Namespace) -> None:
    set_target_backend(args.target_backend)
    set_optimizer_backend(args.optimizer_backend)
    if args.model:
        set_target_deployment(args.model)
    else:
        set_target_deployment(default_model_for_backend(args.target_backend))
    if args.optimizer_model:
        set_optimizer_deployment(args.optimizer_model)
    else:
        set_optimizer_deployment(default_model_for_backend(args.optimizer_backend))
    configure_claude_code_exec(
        path=args.claude_code_exec_path,
        effort=args.claude_code_exec_effort,
    )


def main() -> None:
    args = parse_args()
    skill_content, skill_files = _collect_skill(args.skill)
    try:
        items = load_tasks(args.tasks, limit=args.limit)
    except (ValueError, OSError) as exc:
        sys.exit(f"error: invalid tasks file: {exc}")

    _configure_backends(args)
    os.makedirs(args.out_root, exist_ok=True)

    print(f"[skilleval] skill: {args.skill}"
          + (f" (+{len(skill_files)} supporting files)" if skill_files else ""))
    print(f"[skilleval] tasks: {len(items)} from {args.tasks}")

    rollout_results = run_batch(
        items,
        skill_content,
        args.out_root,
        workers=args.workers,
        timeout=args.timeout,
        model=args.model,
        skill_files=skill_files,
    )

    print(f"[skilleval] judging {len(rollout_results)} responses")
    results = merge_scores(items, rollout_results, judge)

    results_path = os.path.join(args.out_root, "results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    report = build_report(results)
    report_path = os.path.join(args.out_root, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    passed = sum(1 for r in results if r.get("hard"))
    print(f"[skilleval] done: {passed}/{len(results)} passed")
    print(f"[skilleval] report: {report_path}")
    print(f"[skilleval] results: {results_path}")


if __name__ == "__main__":
    main()
