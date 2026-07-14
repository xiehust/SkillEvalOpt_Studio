#!/usr/bin/env python3
"""SkillOpt skilleval: generate an evaluation task set for one or more skills.

Points a claude/codex CLI agent at skill documents and asks it to author
``count`` skilleval task items (id/question/rubric[/files/task_type]).  The
agent WRITES ``generated_tasks.json`` into its working directory — files are
far more reliable than parsing JSON out of chatty stdout.  The file is then
validated with the same ``load_tasks`` the eval/train CLIs use; a validation
failure is fed back to the agent for one retry before the run fails.

Usage
-----
    python3 scripts/generate_tasks.py \
        --skill ~/.claude/skills/my-skill \
        --skill ~/.claude/skills/related-skill \
        --backend claude_code_exec \
        --count 5 \
        --out_root outputs/taskgen_myskill
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import NamedTuple

import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from skillopt.envs.skilleval.dataloader import load_tasks
from skillopt.model.codex_harness import run_claude_code_exec, run_codex_exec
from skillopt.model.common import default_model_for_backend

EXEC_BACKENDS = ("claude_code_exec", "codex_exec")
OUTPUT_FILENAME = "generated_tasks.json"
MAX_SKILL_CHARS = 16000
MAX_TOTAL_SKILL_CHARS = 64000
MAX_ATTEMPTS = 2


class SkillDocument(NamedTuple):
    name: str
    path: str
    content: str
    support_files: list[str]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SkillOpt skilleval — generate a task set for skills")
    p.add_argument(
        "--skill",
        type=str,
        action="append",
        required=True,
        help="Skill to generate tasks for: a markdown file, or a skill directory "
             "containing SKILL.md. Repeat for a unified multi-skill task set.",
    )
    p.add_argument("--backend", type=str, default="claude_code_exec", choices=EXEC_BACKENDS,
                   help="Exec backend that authors the tasks")
    p.add_argument("--model", type=str, default="",
                   help="Model override; empty = backend default "
                        "(codex_exec: the codex CLI's own configured default)")
    p.add_argument("--count", type=int, default=5,
                   help="Number of tasks to generate")
    p.add_argument("--guidance", type=str, default="",
                   help="Optional free-text guidance folded into the generation prompt")
    p.add_argument("--timeout", type=int, default=900,
                   help="Agent timeout in seconds per attempt")
    p.add_argument("--out_root", type=str, required=True,
                   help="Output directory for generated_tasks.json / gen_summary.json")
    return p.parse_args()


def collect_skill(path: str) -> tuple[str, list[str]]:
    """Resolve --skill into (SKILL.md content, supporting file names)."""
    if os.path.isdir(path):
        skill_md_path = os.path.join(path, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            sys.exit(f"error: skill directory has no SKILL.md: {path}")
        support = sorted(
            os.path.relpath(os.path.join(root, name), path)
            for root, _dirs, names in os.walk(path)
            if not any(part.startswith(".") for part in os.path.relpath(root, path).split(os.sep))
            for name in names
            if name != "SKILL.md" and not name.startswith(".")
        )
        return _read_text(skill_md_path), support
    return _read_text(path), []


def collect_skill_document(path: str) -> SkillDocument:
    content, support_files = collect_skill(path)
    return SkillDocument(
        name=_parse_skill_name(content, path),
        path=path,
        content=content,
        support_files=support_files,
    )


def _parse_skill_name(content: str, path: str) -> str:
    text = content.lstrip("\ufeff")
    if text.startswith("---"):
        parts = text.split("\n---", 2)
        if len(parts) >= 2:
            try:
                frontmatter = yaml.safe_load(parts[0].lstrip("-").lstrip("\n"))
            except yaml.YAMLError:
                frontmatter = None
            if isinstance(frontmatter, dict):
                name = frontmatter.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    base = os.path.basename(os.path.normpath(path))
    if not os.path.isdir(path):
        base = os.path.splitext(base)[0]
    return base or "skill"


def _read_text(path: str) -> str:
    if not os.path.isfile(path):
        sys.exit(f"error: skill file not found: {path}")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        sys.exit(f"error: skill file is empty: {path}")
    return content


def build_prompt(
    skill_content: str,
    support_files: list[str],
    count: int,
    guidance: str = "",
    feedback: str | None = None,
) -> str:
    """Backward-compatible single-skill generation prompt."""
    skill = SkillDocument(
        name="skill",
        path="",
        content=skill_content,
        support_files=support_files,
    )
    return _build_prompt([skill], count, guidance, feedback)


def build_multi_skill_prompt(
    skills: list[SkillDocument],
    count: int,
    guidance: str = "",
    feedback: str | None = None,
) -> str:
    """Unified generation prompt for multiple named skills."""
    if len(skills) < 2:
        raise ValueError("build_multi_skill_prompt requires at least two skills")
    return _build_prompt(skills, count, guidance, feedback)


def _build_prompt(
    skills: list[SkillDocument],
    count: int,
    guidance: str,
    feedback: str | None,
) -> str:
    """Generation prompt implementation shared by single and multi-skill modes."""
    multi = len(skills) > 1
    per_skill_budget = min(
        MAX_SKILL_CHARS,
        max(2000, MAX_TOTAL_SKILL_CHARS // max(1, len(skills))),
    )
    parts = [
        (
            "You are designing one unified evaluation task set for a collection of "
            "agent skills from the same plugin."
            if multi
            else "You are designing an evaluation task set for an agent skill."
        ),
        "",
        "## Skills under evaluation" if multi else "## Skill under evaluation",
        "",
    ]
    for skill in skills:
        skill_text = skill.content
        if len(skill_text) > per_skill_budget:
            skill_text = skill_text[:per_skill_budget] + "\n\n[... skill truncated for prompt ...]"
        if multi:
            parts += [f"### Skill: {skill.name}", "", skill_text]
        else:
            parts += [skill_text]
        if skill.support_files:
            parts += [
                "",
                f"Supporting files shipped with {skill.name}: "
                + ", ".join(skill.support_files),
            ]
        if multi:
            parts.append("")
    parts += [
        "## Your job",
        "",
        f"Author exactly {count} evaluation tasks that test whether an agent equipped with "
        + (
            "these skills selects and follows the right skill or combination of skills. "
            "Balance coverage across the collection. Include direct per-skill cases, natural "
            "routing/disambiguation cases for overlapping skills, and cross-skill integration "
            "cases only where the documented workflows genuinely compose. "
            if multi
            else "this skill actually follows it — cover the skill's core workflow plus edge cases. "
        )
        + (
            "When the requested count is at least the number of skills, every skill must be "
            "covered by at least one task. "
            if multi
            else ""
        )
        + "Do not name the expected skill in the question unless explicit skill invocation is "
        "itself the behavior being tested. "
        "Each task must be completable inside an isolated working directory with no network "
        "access and no external accounts; if a task needs input data, provide it inline via "
        'its "files" field.',
        "",
        "## Output format (STRICT)",
        "",
        f"Write ONE file named `{OUTPUT_FILENAME}` in the current working directory: a UTF-8 "
        "JSON array where each element has:",
        '- "id" (string, required): unique, filesystem-safe (no \'/\', \'\\\', \'..\'); use task_001 style',
        '- "question" (string, required): the task text given to the agent being evaluated '
        "(the agent sees the skill, not the rubric)",
        '- "rubric" (string, required): acceptance criteria for an LLM judge — objectively '
        "checkable from the agent's response/artifacts alone, never vague quality adjectives",
        '- "files" (object, optional): {relative path: text content} seeded into the agent\'s working directory',
        (
            '- "target_skills" (array of strings, required): one or more exact skill names '
            "from the collection above that should handle the task"
            if multi
            else '- "task_type" (string, optional): grouping key'
        ),
        (
            '- "task_type" (string, required): use the primary target skill name for a '
            'single-skill task, or "integration" for a genuine multi-skill task'
            if multi
            else ""
        ),
        "",
        f"Do NOT print the JSON to stdout — write the `{OUTPUT_FILENAME}` file.",
    ]
    if guidance.strip():
        parts += ["", "## User guidance", "", guidance.strip()]
    if feedback:
        parts += [
            "",
            "## Previous attempt failed validation",
            "",
            f"{feedback}",
            f"Fix the problem and rewrite `{OUTPUT_FILENAME}` completely.",
        ]
    return "\n".join(parts)


def validate_generated_tasks(
    tasks: list[dict],
    requested_count: int,
    skills: list[SkillDocument],
) -> None:
    if len(tasks) != requested_count:
        raise ValueError(
            f"expected exactly {requested_count} generated tasks, got {len(tasks)}"
        )
    if len(skills) < 2:
        return

    allowed = {skill.name for skill in skills}
    covered: set[str] = set()
    for index, task in enumerate(tasks):
        targets = task.get("target_skills")
        if not isinstance(targets, list) or not targets:
            raise ValueError(
                f"item #{index} (id={task.get('id')!r}): 'target_skills' must be "
                "a non-empty array in multi-skill mode"
            )
        if not all(isinstance(target, str) and target.strip() for target in targets):
            raise ValueError(
                f"item #{index} (id={task.get('id')!r}): every target_skills entry "
                "must be a non-empty string"
            )
        unknown = set(targets) - allowed
        if unknown:
            raise ValueError(
                f"item #{index} (id={task.get('id')!r}): unknown target_skills "
                f"{sorted(unknown)}; expected names from {sorted(allowed)}"
            )
        task_type = task.get("task_type")
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError(
                f"item #{index} (id={task.get('id')!r}): 'task_type' is required "
                "in multi-skill mode"
            )
        covered.update(targets)

    if requested_count >= len(skills) and covered != allowed:
        raise ValueError(
            "multi-skill task set does not cover every skill; missing "
            f"{sorted(allowed - covered)}"
        )


def run_agent(backend: str, work_dir: str, prompt: str, model: str, timeout: int) -> str:
    """Dispatch one generation attempt to the exec backend; returns the response text."""
    if backend == "codex_exec":
        response, _raw = run_codex_exec(
            work_dir=work_dir, prompt=prompt, model=model, timeout=timeout,
        )
    else:
        response, _raw = run_claude_code_exec(
            work_dir=work_dir, prompt=prompt, model=model, timeout=timeout,
            allowed_tools="Read,Bash,Write,Edit,Glob,Grep",
            allow_file_edits=True,
        )
    return response


def main() -> None:
    args = parse_args()
    if args.count < 1:
        sys.exit(f"error: --count must be >= 1, got {args.count}")
    skill_paths = list(dict.fromkeys(args.skill))
    skills = [collect_skill_document(path) for path in skill_paths]
    skill_names = [skill.name for skill in skills]
    if len(set(skill_names)) != len(skill_names):
        sys.exit(f"error: skill names must be unique, got {skill_names}")
    model = args.model
    if not model and args.backend != "codex_exec":
        model = default_model_for_backend(args.backend)

    out_root = os.path.abspath(args.out_root)  # codex -C double-resolves relative paths
    work_dir = os.path.join(out_root, "gen_workspace")
    os.makedirs(work_dir, exist_ok=True)
    out_file = os.path.join(work_dir, OUTPUT_FILENAME)

    print(f"[taskgen] skills: {len(skills)}")
    for skill in skills:
        print(
            f"  - {skill.name}: {skill.path}"
            + (
                f" (+{len(skill.support_files)} supporting files)"
                if skill.support_files
                else ""
            )
        )
    print(f"[taskgen] backend: {args.backend}  model: {model or '(CLI default)'}  count: {args.count}")

    start = time.time()
    tasks: list[dict] | None = None
    feedback: str | None = None
    attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        print(f"[taskgen] attempt {attempt}/{MAX_ATTEMPTS}", flush=True)
        if len(skills) > 1:
            prompt = build_multi_skill_prompt(
                skills, args.count, args.guidance, feedback
            )
        else:
            prompt = build_prompt(
                skills[0].content,
                skills[0].support_files,
                args.count,
                args.guidance,
                feedback,
            )
        run_agent(args.backend, work_dir, prompt, model, args.timeout)
        try:
            if not os.path.isfile(out_file):
                raise ValueError(f"agent did not write {OUTPUT_FILENAME} in its working directory")
            tasks = load_tasks(out_file)
            validate_generated_tasks(tasks, args.count, skills)
            break
        except ValueError as exc:
            feedback = str(exc)
            print(f"[taskgen] validation failed: {feedback}", flush=True)
            if os.path.isfile(out_file):
                os.replace(out_file, f"{out_file}.attempt{attempt}.invalid")

    if tasks is None:
        sys.exit(f"error: generated tasks failed validation after {MAX_ATTEMPTS} attempts: {feedback}")

    duration_s = round(time.time() - start, 1)
    os.makedirs(out_root, exist_ok=True)
    final_path = os.path.join(out_root, OUTPUT_FILENAME)
    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    summary = {
        "count": len(tasks),
        "requested_count": args.count,
        "backend": args.backend,
        "model": model,
        "skill": skill_paths[0] if len(skill_paths) == 1 else None,
        "skills": skill_paths,
        "skill_names": skill_names,
        "skill_count": len(skills),
        "attempts": attempts,
        "duration_s": duration_s,
    }
    with open(os.path.join(out_root, "gen_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[taskgen] done: {len(tasks)} tasks in {duration_s}s -> {final_path}")


if __name__ == "__main__":
    main()
