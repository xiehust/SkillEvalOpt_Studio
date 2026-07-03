"""SkillEval rollout — drive Claude Code CLI on each task under the skill.

Each task gets an isolated work_dir seeded with the skill document (via
``prepare_workspace``, which writes ``.agents/skills/skillopt-target/SKILL.md``)
plus any task-declared files, then ``run_claude_code_exec`` drives the agent.
Failures are isolated per task: one crashing task never aborts the batch, and
rollout adds no retry of its own (``run_claude_code_exec`` owns retries).
"""
from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from skillopt.model.codex_harness import prepare_workspace, run_claude_code_exec

GUIDE_PROMPT = (
    "Read `.agents/skills/skillopt-target/SKILL.md` first and follow it while "
    "working. Relative paths mentioned in the skill (scripts/, references/, "
    "examples/, ...) resolve from `.agents/skills/skillopt-target/`. "
    "Then complete the task described in `task.md`. Give your final "
    "answer or a summary of what you produced at the end of your reply."
)

# where prepare_workspace installs the skill inside each work_dir
SKILL_INSTALL_DIR = os.path.join(".agents", "skills", "skillopt-target")


def _rollout_one(
    item: dict,
    skill_content: str,
    out_root: str,
    *,
    timeout: int,
    model: str,
    skill_files: list[tuple[str, str]] | None = None,
) -> dict:
    work_dir = os.path.join(out_root, "rollouts", item["id"])
    result = {
        "id": str(item["id"]),
        "task_type": item.get("task_type", "default"),
        "response": "",
        "duration_s": 0.0,
        "work_dir": work_dir,
    }
    start = time.time()
    try:
        copy_files = [
            (src, os.path.join(SKILL_INSTALL_DIR, rel_dst))
            for src, rel_dst in (skill_files or [])
        ]
        prepare_workspace(
            work_dir=work_dir,
            skill_md=skill_content,
            task_text=item["question"],
            extra_files=item.get("files") or None,
            copy_files=copy_files or None,
        )
        # Artifact-producing tasks are the norm in skill evaluation: allow file
        # edits (the default exec prompt injects "Do not modify files.") and
        # extend the read-only default tool set accordingly.
        response, _raw = run_claude_code_exec(
            work_dir=work_dir,
            prompt=GUIDE_PROMPT,
            model=model,
            timeout=timeout,
            allowed_tools="Read,Bash,Write,Edit,Glob,Grep",
            allow_file_edits=True,
        )
        result["response"] = response
    except Exception as exc:  # noqa: BLE001 — isolate task failures
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["error_traceback"] = traceback.format_exc(limit=5)
    result["duration_s"] = round(time.time() - start, 2)
    return result


def run_batch(
    items: list[dict],
    skill_content: str,
    out_root: str,
    *,
    workers: int = 4,
    timeout: int = 600,
    model: str = "",
    skill_files: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """Roll out every task in *items* under *skill_content*, input order preserved.

    *skill_files* carries a multi-file skill's supporting files as
    ``(absolute src, path relative to the skill dir)`` pairs; they are copied
    into each work_dir under ``.agents/skills/skillopt-target/`` so relative
    references (scripts/, references/, ...) keep resolving.
    """
    os.makedirs(out_root, exist_ok=True)
    if not items:
        return []

    print(f"  [skilleval] rolling out {len(items)} tasks (workers={workers})")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(
                _rollout_one,
                item,
                skill_content,
                out_root,
                timeout=timeout,
                model=model,
                skill_files=skill_files,
            )
            for item in items
        ]
        results = [future.result() for future in futures]

    failed = sum(1 for r in results if r.get("error"))
    print(f"  [skilleval] rollout finished: {len(results) - failed} ok, {failed} errored")
    return results
