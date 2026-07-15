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

import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from skillopt.envs.skilleval.dataloader import load_tasks
from skillopt.envs.skilleval.evaluator import artifacts_listing, judge, merge_scores  # noqa: F401 — merge_scores re-exported for tests/importers
from skillopt.envs.skilleval.plugin import (
    _collect_skill as _collect_skill_source,
    aggregate_results,
    collect_runtime_skills,
    normalize_plugin_tasks,
    skill_name as _skill_name,
)
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
    p = argparse.ArgumentParser(description="SkillOpt skilleval — evaluate one or more custom skills")
    p.add_argument("--skill", type=str, required=True, action="append",
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


def _collect_skill(path: str) -> tuple[str, list[tuple[str, str]]]:
    """Compatibility wrapper around the shared Plugin Skill collector."""
    try:
        content, files, _source_dir = _collect_skill_source(path)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    return content, files


def _read_skill_file(path: str) -> str:
    """Compatibility helper retained for importers of this CLI module."""
    try:
        content, _files, _source_dir = _collect_skill_source(path)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    return content


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
              "| id | pass | soft | reason | duration (s) |",
              "|---|---|---|---|---|"]
    for r in results:
        skipped = r.get("judge_skipped")
        reason_value = (
            r.get("error")
            or r.get("judge_error")
            or r.get("judge_reason")
            or (f"judge skipped: {skipped}" if skipped else "")
        )
        reason = str(reason_value).replace("|", "\\|").replace("\n", " ")
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
    try:
        runtime_skills = collect_runtime_skills(args.skill)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        sys.exit(f"error: invalid skill: {exc}")
    plugin_mode = len(runtime_skills) > 1
    primary = runtime_skills[0]
    try:
        items = load_tasks(args.tasks)
        normalize_plugin_tasks(items, {skill["name"] for skill in runtime_skills})
        if args.limit and args.limit > 0:
            items = items[:args.limit]
    except (ValueError, OSError) as exc:
        sys.exit(f"error: invalid tasks file: {exc}")

    _configure_backends(args)
    os.makedirs(args.out_root, exist_ok=True)

    print(f"[skilleval] skills: {', '.join(skill['name'] for skill in runtime_skills)}")
    print(f"[skilleval] tasks: {len(items)} from {args.tasks}")

    rollout_results = run_batch(
        items,
        primary["content"],
        args.out_root,
        workers=args.workers,
        timeout=args.timeout,
        model=args.model,
        skill_files=primary["files"],
        runtime_skills=runtime_skills if plugin_mode else None,
    )

    print(f"[skilleval] judging {len(rollout_results)} responses")
    results = merge_scores(items, rollout_results, judge)
    for item, result in zip(items, results):
        result["target_skills"] = list(item.get("target_skills") or [])
        result["task_type"] = item.get("task_type", "default")

    results_path = os.path.join(args.out_root, "results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    summary = aggregate_results(results, [skill["name"] for skill in runtime_skills])
    with open(os.path.join(args.out_root, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)

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
