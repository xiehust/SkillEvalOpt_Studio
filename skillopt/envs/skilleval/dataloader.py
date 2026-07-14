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

from skillopt.datasets.base import (
    SplitDataLoader,
    _compute_weighted_counts,
    _load_json_or_jsonl,
)
from skillopt.envs.skilleval.plugin import normalize_plugin_tasks

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
        if (
            not isinstance(rel_path, str)
            or not rel_path
            or rel_path.startswith(("~", "/", "\\"))
            or "\\" in rel_path
            or any(part in ("", ".", "..") for part in rel_path.split("/"))
        ):
            raise ValueError(
                f"{_item_label(index, item)}: 'files' path {rel_path!r} "
                "must be a safe relative path"
            )
        parts = rel_path.split("/")
        if parts[0] in {".agents", "task.md"}:
            raise ValueError(
                f"{_item_label(index, item)}: 'files' path {rel_path!r} "
                "collides with the evaluation runtime"
            )
        if not isinstance(content, str):
            raise ValueError(
                f"{_item_label(index, item)}: 'files' value for {rel_path!r} "
                f"must be str, got {type(content).__name__}"
            )
    return dict(files)


def _normalize_items(raw_items: list, source: str) -> list[dict]:
    """Validate and normalize raw task items (shared by file and split loading)."""
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError(f"No task items found in {source}")

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

        raw_task_type = item.get("task_type")
        if raw_task_type is not None and not isinstance(raw_task_type, str):
            raise ValueError(
                f"{_item_label(index, item)}: 'task_type' must be a string"
            )
        normalized = dict(item)
        normalized["files"] = _validate_files(index, item)
        normalized["task_type"] = raw_task_type or _DEFAULT_TASK_TYPE
        tasks.append(normalized)
    return tasks


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
    tasks = _normalize_items(_load_json_or_jsonl(path), path)
    if limit and limit > 0:
        tasks = tasks[:limit]
    return tasks


class SkillEvalDataLoader(SplitDataLoader):
    """Split-based task loading for training on skilleval task sets.

    Each split directory (train/, val/, test/) holds one JSON array of task
    items with the same schema (and the same fail-fast validation) as the
    single-file evaluation path.
    """

    def setup(self, cfg: dict) -> None:
        self.plugin_skill_names = self._normalize_skill_names(
            cfg.get("plugin_skill_names"),
            "plugin_skill_names",
        )
        self.required_validation_skills = self._normalize_skill_names(
            cfg.get("required_validation_skills"),
            "required_validation_skills",
        )
        installed = set(self.plugin_skill_names)
        unknown = [
            name
            for name in self.required_validation_skills
            if name not in installed
        ]
        if unknown:
            raise ValueError(
                "required_validation_skills must be installed Plugin Skills: "
                f"{unknown}"
            )
        self._minimum_validation_count = 0
        super().setup(cfg)

    @staticmethod
    def _normalize_skill_names(value: object, key: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{key} must be a string array")
        names: list[str] = []
        for raw_name in value:
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError(f"{key} must contain non-empty strings")
            name = raw_name.strip()
            if name not in names:
                names.append(name)
        return tuple(names)

    def _normalize_plugin_metadata(self, items: list[dict]) -> list[dict]:
        if self.plugin_skill_names:
            normalize_plugin_tasks(items, set(self.plugin_skill_names))
        return items

    def load_raw_items(self, data_path: str) -> list[dict]:
        items = _normalize_items(_load_json_or_jsonl(data_path), data_path)
        return self._normalize_plugin_metadata(items)

    def load_split_items(self, split_path: str) -> list[dict]:
        items = super().load_split_items(split_path)
        normalized = _normalize_items(items, split_path)
        return self._normalize_plugin_metadata(normalized)

    def _partition_ratio_items(
        self,
        shuffled: list[dict],
        counts: tuple[int, int, int],
        ratio: tuple[int, int, int],
    ) -> tuple[list[dict], list[dict], list[dict]]:
        required_names = self.required_validation_skills
        if not required_names:
            return super()._partition_ratio_items(shuffled, counts, ratio)

        required = set(required_names)
        coverage = [
            {
                name
                for name in item.get("target_skills", [])
                if isinstance(name, str) and name in required
            }
            for item in shuffled
        ]
        covered = {name for targets in coverage for name in targets}
        missing = [name for name in required_names if name not in covered]
        if missing:
            raise ValueError(
                "Plugin task set cannot cover every trainable Skill; "
                f"missing source coverage for: {missing}"
            )

        selected: list[int] = []
        selected_set: set[int] = set()
        uncovered = set(required)
        while uncovered:
            best_index = -1
            best_gain: set[str] = set()
            for index, targets in enumerate(coverage):
                if index in selected_set:
                    continue
                gain = targets & uncovered
                if len(gain) > len(best_gain):
                    best_index = index
                    best_gain = gain
            if best_index < 0:
                raise RuntimeError(
                    "coverage selection stalled after source coverage validation"
                )
            selected.append(best_index)
            selected_set.add(best_index)
            uncovered -= best_gain

        self._minimum_validation_count = len(selected)
        val_n = max(counts[1], self._minimum_validation_count)
        if val_n > len(shuffled) - 2:
            raise ValueError(
                "Plugin ratio split cannot keep train and test non-empty: "
                f"validation needs {self._minimum_validation_count} tasks to cover "
                f"{list(required_names)}, but the source has {len(shuffled)} tasks"
            )

        for index in range(len(shuffled)):
            if len(selected) >= val_n:
                break
            if index not in selected_set:
                selected.append(index)
                selected_set.add(index)

        val_items = [shuffled[index] for index in selected]
        remaining = [
            item for index, item in enumerate(shuffled) if index not in selected_set
        ]
        train_n = _compute_weighted_counts(
            len(remaining),
            (ratio[0], ratio[2]),
        )[0]
        train_n = max(1, min(len(remaining) - 1, train_n))
        train_items = remaining[:train_n]
        test_items = remaining[train_n:]
        return train_items, val_items, test_items

    def _ratio_split_manifest(self) -> dict:
        if not self.required_validation_skills:
            return {}
        return {
            "coverage_aware": True,
            "required_validation_skills": list(self.required_validation_skills),
            "minimum_validation_count": self._minimum_validation_count,
        }
