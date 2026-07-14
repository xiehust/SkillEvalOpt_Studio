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
import re
import sys

import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from skillopt.envs.skilleval.dataloader import load_tasks
from skillopt.envs.skilleval.evaluator import artifacts_listing, judge, merge_scores  # noqa: F401 — merge_scores re-exported for tests/importers
from skillopt.envs.skilleval.rollout import RuntimeSkill, collect_support_files, run_batch
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
    """Resolve --skill into (SKILL.md content, supporting files).

    A file path is single-file mode (no supporting files). A directory must
    contain SKILL.md; every other regular file in it is collected by
    ``collect_support_files`` (hidden entries, tooling caches, and symlinks
    are skipped — a workspace must never reach back into the source skill).
    """
    if os.path.isdir(path):
        skill_md_path = os.path.join(path, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            sys.exit(f"error: skill directory has no SKILL.md: {path}")
        return _read_skill_file(skill_md_path), collect_support_files(path)
    return _read_skill_file(path), []


def _skill_name(content: str, path: str) -> str:
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", content, re.DOTALL)
    if match:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()
    if os.path.isdir(path):
        return os.path.basename(os.path.normpath(path))
    return os.path.splitext(os.path.basename(path))[0]


def collect_runtime_skills(paths: list[str]) -> list[RuntimeSkill]:
    skills: list[RuntimeSkill] = []
    seen: set[str] = set()
    for path in paths:
        content, files = _collect_skill(path)
        name = _skill_name(content, path)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError(f"skill name must be filesystem-safe: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate skill name: {name!r}")
        seen.add(name)
        skills.append({"name": name, "content": content, "files": files})
    return skills


def normalize_plugin_tasks(items: list[dict], skill_names: set[str]) -> None:
    for index, item in enumerate(items):
        raw = item.get("target_skills")
        if raw is None:
            item["target_skills"] = []
        elif (
            not isinstance(raw, list)
            or not raw
            or any(not isinstance(name, str) or not name.strip() for name in raw)
        ):
            raise ValueError(f"item #{index} target_skills must be a non-empty string array")
        else:
            targets = list(dict.fromkeys(name.strip() for name in raw))
            unknown = sorted(set(targets) - skill_names)
            if unknown:
                raise ValueError(f"item #{index} target_skills contains unknown skills: {unknown}")
            item["target_skills"] = targets
        raw_task_type = item.get("task_type")
        if raw_task_type is None:
            item["task_type"] = "default"
        elif not isinstance(raw_task_type, str):
            raise ValueError(f"item #{index} task_type must be a string")
        else:
            item["task_type"] = raw_task_type.strip() or "default"


def _read_skill_file(path: str) -> str:
    if not os.path.isfile(path):
        sys.exit(f"error: skill file not found: {path}")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        sys.exit(f"error: skill file is empty: {path}")
    return content


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _metrics(results: list[dict]) -> dict:
    return {
        "count": len(results),
        "hard": _mean([float(result.get("hard", 0)) for result in results]),
        "soft": _mean([float(result.get("soft", 0.0)) for result in results]),
    }


def aggregate_results(results: list[dict], skill_names: list[str]) -> dict:
    by_skill = {
        name: _metrics([result for result in results if name in result.get("target_skills", [])])
        for name in skill_names
    }
    by_type = {
        task_type: _metrics([result for result in results if result.get("task_type", "default") == task_type])
        for task_type in sorted({str(result.get("task_type", "default")) for result in results})
    }
    routing = [result for result in results if result.get("task_type") == "routing"]
    integration = [
        result for result in results
        if result.get("task_type") == "integration" or len(result.get("target_skills", [])) > 1
    ]
    weakest = min(
        ((name, metrics) for name, metrics in by_skill.items() if metrics["count"]),
        key=lambda entry: (entry[1]["hard"], entry[1]["soft"], entry[0]),
        default=None,
    )
    return {
        "mode": "plugin" if len(skill_names) > 1 else "skill",
        "skill_count": len(skill_names),
        "skill_names": list(skill_names),
        "overall": _metrics(results),
        "by_skill": by_skill,
        "by_task_type": by_type,
        "routing": _metrics(routing) if routing else None,
        "integration": _metrics(integration) if integration else None,
        "weakest_skill": (
            {"name": weakest[0], **weakest[1]} if weakest is not None else None
        ),
    }


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
