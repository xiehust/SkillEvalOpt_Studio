"""Task set storage: validated skilleval task files under studio_root/tasksets/.

Validation is delegated to :func:`skillopt.envs.skilleval.dataloader.load_tasks`
so the studio accepts exactly what the eval/train CLIs accept — fail-fast, with
the offending item named in the error.  A validation failure leaves no partial
task set on disk.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from skillopt.envs.skilleval.dataloader import load_tasks

from skillopt_studio.config import StudioConfig
from skillopt_studio.models import TaskSetInfo
from skillopt_studio.skill_sources import slugify

SPLIT_NAMES = ("train", "val", "test")

_META_FILE = "meta.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expected_files(mode: str, files: dict[str, bytes]) -> dict[str, bytes]:
    """Map incoming file keys onto the canonical on-disk names for the mode."""
    if mode == "single":
        if set(files) != {"tasks"}:
            raise ValueError(
                f"single mode expects exactly one file keyed 'tasks', got {sorted(files)}"
            )
        return {"tasks.json": files["tasks"]}
    if mode == "split":
        missing = [name for name in ("train", "val") if name not in files]
        if missing:
            raise ValueError(f"split mode requires 'train' and 'val' files; missing {missing}")
        unknown = [key for key in files if key not in SPLIT_NAMES]
        if unknown:
            raise ValueError(f"split mode accepts only {list(SPLIT_NAMES)}; got extra {unknown}")
        return {f"{split}.json": files[split] for split in SPLIT_NAMES if split in files}
    raise ValueError(f"mode must be 'single' or 'split', got {mode!r}")


def save_taskset(
    config: StudioConfig,
    name: str,
    files: dict[str, bytes],
    mode: str,
    *,
    display_name: str | None = None,
    sample: bool = False,
) -> TaskSetInfo:
    """Persist and validate a task set; raises ValueError on any invalid input.

    ``display_name`` decouples the shown name from the id-deriving ``name``
    (used by sample materialization to pin ids like ``sample-searchqa`` while
    showing a Chinese title).  ``sample`` marks the set read-only for users.
    """
    slug = slugify(name)
    on_disk = _expected_files(mode, files)
    target_dir = config.tasksets_dir / slug
    if target_dir.exists():
        raise ValueError(f"task set {slug!r} already exists; delete it first")

    target_dir.mkdir(parents=True)
    try:
        counts_by_split: dict[str, int] = {}
        for filename, payload in on_disk.items():
            path = target_dir / filename
            path.write_bytes(payload)
            tasks = load_tasks(str(path))
            counts_by_split[path.stem] = len(tasks)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    info = TaskSetInfo(
        id=slug,
        name=display_name or name,
        mode=mode,  # type: ignore[arg-type]
        task_count=sum(counts_by_split.values()),
        counts_by_split=counts_by_split,
        created_at=_now_iso(),
        sample=sample,
    )
    (target_dir / _META_FILE).write_text(
        json.dumps(info.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return info


def _serialize_tasks(items: list[dict]) -> bytes:
    return json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")


def create_taskset_from_items(
    config: StudioConfig,
    name: str,
    mode: str,
    tasks_by_split: dict[str, list[dict]],
    *,
    display_name: str | None = None,
    sample: bool = False,
) -> TaskSetInfo:
    """Create a task set from in-memory items (manual editor / AI import path).

    Split keys follow the file-upload convention: single mode uses 'tasks',
    split mode uses train/val(/test).  Validation and atomicity are exactly
    :func:`save_taskset`'s — a bad item leaves nothing on disk.
    """
    files = {split: _serialize_tasks(items) for split, items in tasks_by_split.items()}
    return save_taskset(config, name, files, mode, display_name=display_name, sample=sample)


def update_taskset(
    config: StudioConfig,
    taskset_id: str,
    tasks_by_split: dict[str, list[dict]],
    name: str | None = None,
) -> TaskSetInfo:
    """Full-replace update; validates every split before touching stored files.

    ``tasks_by_split`` carries ALL splits that should exist after the edit
    (single: 'tasks'; split: train+val required, omitting test deletes an
    existing test.json).  All splits are written to ``*.tmp`` and validated
    with :func:`load_tasks` first, then swapped in with ``os.replace`` — a
    validation failure leaves the stored files and meta byte-identical.
    Raises KeyError when the task set does not exist, ValueError on any
    invalid input.
    """
    info = get_taskset(config, taskset_id)
    if info is None:
        raise KeyError(taskset_id)
    _reject_sample_write(info)
    directory = _taskset_dir(config, taskset_id)
    serialized = {split: _serialize_tasks(items) for split, items in tasks_by_split.items()}
    on_disk = _expected_files(info.mode, serialized)

    counts_by_split: dict[str, int] = {}
    tmp_by_final: dict[Path, Path] = {}
    try:
        for filename, payload in on_disk.items():
            final = directory / filename
            tmp = directory / f"{filename}.tmp"
            tmp.write_bytes(payload)
            tmp_by_final[final] = tmp
            counts_by_split[final.stem] = len(load_tasks(str(tmp)))
    except Exception:
        for tmp in tmp_by_final.values():
            tmp.unlink(missing_ok=True)
        raise
    for final, tmp in tmp_by_final.items():
        os.replace(tmp, final)
    if info.mode == "split":
        for split in SPLIT_NAMES:
            stale = directory / f"{split}.json"
            if f"{split}.json" not in on_disk and stale.exists():
                stale.unlink()

    updated = info.model_copy(
        update={
            "name": name.strip() if name and name.strip() else info.name,
            "task_count": sum(counts_by_split.values()),
            "counts_by_split": counts_by_split,
            "updated_at": _now_iso(),
        }
    )
    (directory / _META_FILE).write_text(
        json.dumps(updated.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return updated


def _reject_sample_write(info: TaskSetInfo) -> None:
    if info.sample:
        raise ValueError(
            f"任务集 {info.id!r} 是内置样例，只读；请在详情页「另存为我的任务集」后编辑副本"
        )


def list_tasksets(config: StudioConfig) -> list[TaskSetInfo]:
    """All task sets; sample entries are hidden while samples are disabled
    (a stale materialization on disk must not resurface after the switch-off)."""
    tasksets: list[TaskSetInfo] = []
    root = config.tasksets_dir
    if not root.is_dir():
        return tasksets
    for entry in sorted(root.iterdir()):
        meta_path = entry / _META_FILE
        if not meta_path.is_file():
            continue
        try:
            info = TaskSetInfo(**json.loads(meta_path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            continue  # a corrupt meta.json hides that entry, never the whole list
        if info.sample and not config.samples_enabled:
            continue
        tasksets.append(info)
    return tasksets


def _taskset_dir(config: StudioConfig, taskset_id: str) -> Path:
    slug = slugify(taskset_id)
    if slug != taskset_id:
        raise ValueError(f"invalid task set id {taskset_id!r}")
    return config.tasksets_dir / slug


def get_taskset(
    config: StudioConfig, taskset_id: str, include_samples: bool = False
) -> TaskSetInfo | None:
    """One task set's meta; sample entries are hidden while samples are disabled
    unless ``include_samples`` (the materializer needs to see stale ones)."""
    try:
        meta_path = _taskset_dir(config, taskset_id) / _META_FILE
    except ValueError:
        return None
    if not meta_path.is_file():
        return None
    info = TaskSetInfo(**json.loads(meta_path.read_text(encoding="utf-8")))
    if info.sample and not config.samples_enabled and not include_samples:
        return None
    return info


def get_taskset_tasks(config: StudioConfig, taskset_id: str, preview: int = 0) -> dict[str, list[dict]]:
    """Tasks per split (single mode uses key 'tasks'); preview>0 caps each list."""
    info = get_taskset(config, taskset_id)
    if info is None:
        raise KeyError(taskset_id)
    directory = _taskset_dir(config, taskset_id)
    tasks_by_split: dict[str, list[dict]] = {}
    filenames = ["tasks.json"] if info.mode == "single" else [f"{s}.json" for s in SPLIT_NAMES]
    for filename in filenames:
        path = directory / filename
        if not path.is_file():
            continue
        tasks = load_tasks(str(path))
        tasks_by_split[path.stem] = tasks[:preview] if preview > 0 else tasks
    return tasks_by_split


def taskset_file_paths(config: StudioConfig, taskset_id: str) -> dict[str, Path]:
    """Absolute paths of the stored task files, keyed by split name (for runners)."""
    info = get_taskset(config, taskset_id)
    if info is None:
        raise KeyError(taskset_id)
    directory = _taskset_dir(config, taskset_id)
    filenames = ["tasks.json"] if info.mode == "single" else [f"{s}.json" for s in SPLIT_NAMES]
    return {Path(f).stem: directory / f for f in filenames if (directory / f).is_file()}


def delete_taskset(config: StudioConfig, taskset_id: str) -> bool:
    """Delete a user task set.  Sample task sets raise ValueError (read-only)."""
    info = get_taskset(config, taskset_id)
    if info is None:
        return False
    _reject_sample_write(info)
    shutil.rmtree(_taskset_dir(config, taskset_id))
    return True
