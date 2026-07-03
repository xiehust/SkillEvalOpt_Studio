"""SkillEval task set loading and validation.

Unlike benchmark envs, skilleval consumes a single user-provided task file
(JSON array or JSONL) rather than a train/val/test split directory.  Each
task item must carry its own acceptance criteria (``rubric``) for the LLM
judge.  Validation is fail-fast: any malformed item aborts the run before
a single model call is spent.

Task item schema::

    {
      "id": "task_001",            # required, unique, filesystem-safe
      "question": "...",           # required — task text given to the agent
      "rubric": "...",             # required — judge acceptance criteria
      "files": {"rel/path": "…"},  # optional — seeded into the work_dir
      "task_type": "..."           # optional — grouping key, default "default"
    }
"""
from __future__ import annotations

from skillopt.datasets.base import _load_json_or_jsonl

_REQUIRED_FIELDS = ("id", "question", "rubric")
_DEFAULT_TASK_TYPE = "default"


def _item_label(index: int, item: dict) -> str:
    """Human-readable locator for error messages: index plus id when present."""
    raw_id = item.get("id") if isinstance(item, dict) else None
    if isinstance(raw_id, str) and raw_id:
        return f"item #{index} (id={raw_id!r})"
    return f"item #{index}"


def _validate_id(index: int, item: dict) -> str:
    task_id = item["id"]
    if "/" in task_id or "\\" in task_id or ".." in task_id:
        raise ValueError(
            f"{_item_label(index, item)}: id must be filesystem-safe "
            "(no '/', '\\', or '..') because it names the task work_dir"
        )
    return task_id


def _validate_files(index: int, item: dict) -> dict:
    files = item.get("files")
    if files is None:
        return {}
    if not isinstance(files, dict):
        raise ValueError(
            f"{_item_label(index, item)}: 'files' must be a dict of "
            f"{{relative path: text content}}, got {type(files).__name__}"
        )
    for rel_path, content in files.items():
        if not isinstance(content, str):
            raise ValueError(
                f"{_item_label(index, item)}: 'files' value for {rel_path!r} "
                f"must be str, got {type(content).__name__}"
            )
    return dict(files)


def load_tasks(path: str, limit: int = 0) -> list[dict]:
    """Load and validate a skilleval task file (JSON array or JSONL).

    The entire file is validated before any slicing so a corrupt item fails
    the run deterministically regardless of ``limit``.

    Raises
    ------
    ValueError
        On any missing/empty required field, duplicate id, unsafe id, or
        non-str ``files`` value.  The message names the offending item.
    """
    raw_items = _load_json_or_jsonl(path)
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError(f"No task items found in {path}")

    tasks: list[dict] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(
                f"item #{index}: expected an object, got {type(item).__name__}"
            )
        for field_name in _REQUIRED_FIELDS:
            value = item.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{_item_label(index, item)}: missing or empty required "
                    f"field {field_name!r}"
                )
        task_id = _validate_id(index, item)
        if task_id in seen_ids:
            raise ValueError(
                f"{_item_label(index, item)}: duplicate id {task_id!r}"
            )
        seen_ids.add(task_id)

        normalized = dict(item)
        normalized["files"] = _validate_files(index, item)
        normalized["task_type"] = str(item.get("task_type") or _DEFAULT_TASK_TYPE)
        tasks.append(normalized)

    if limit and limit > 0:
        tasks = tasks[:limit]
    return tasks
