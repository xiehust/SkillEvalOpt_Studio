"""Command builders for real eval/train runs.

Pure-ish logic (writes only inside the given job_dir) so it is fully unit
testable with stub CLIs: tests monkeypatch ``EVAL_SCRIPT`` / ``TRAIN_SCRIPT``
to fake scripts that write realistic artifacts in seconds.

Train configs are never hand-templated: ``skillopt.config.load_config``
parses ``configs/skilleval/default.yaml`` (resolving ``_base_``), overrides
are applied on the structured dict, and the result is dumped to
``<job_dir>/config.yaml`` — so studio configs can't drift from what
scripts/train.py actually accepts.
"""
from __future__ import annotations

import json
import random
import re
import shlex
import shutil
import sys
from pathlib import Path

import yaml

from skillopt.config import load_config as load_structured_config
from skillopt.envs.skilleval.coverage import (
    PLUGIN_MIN_TASKS_PER_SKILL,
    minimum_plugin_task_count,
    plan_disjoint_plugin_coverage,
    target_skill_counts,
)
from skillopt.envs.skilleval.plugin import (
    collect_plugin_state,
    normalize_plugin_tasks,
)
from skillopt.model.common import default_model_for_backend

from skillopt_studio import skill_sources, tasksets
from skillopt_studio.config import StudioConfig
from skillopt_studio.models import (
    PluginCoverageReport,
    PluginSkillCoverage,
    SkillInfo,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Module-level so tests can monkeypatch them to stub CLIs.
EVAL_SCRIPT = PROJECT_ROOT / "scripts" / "evaluate_skill.py"
TRAIN_SCRIPT = PROJECT_ROOT / "scripts" / "train.py"
PLUGIN_TRAIN_SCRIPT = PROJECT_ROOT / "scripts" / "train_plugin.py"
GEN_SCRIPT = PROJECT_ROOT / "scripts" / "generate_tasks.py"
TRAIN_BASE_CONFIG = PROJECT_ROOT / "configs" / "skilleval" / "default.yaml"
BUNDLE_MODULE = "skillopt.envs.skilleval.bundle"

PYTHON = sys.executable


def _resolve_skill(config: StudioConfig, skill_id: str) -> SkillInfo:
    skill = skill_sources.get_skill(config, skill_id)
    if skill is None:
        raise ValueError(f"skill {skill_id!r} not found")
    return skill


def _resolve_taskset_file(config: StudioConfig, taskset_id: str) -> Path:
    """The single task file an eval run consumes (split sets prefer test)."""
    try:
        paths = tasksets.taskset_file_paths(config, taskset_id)
    except KeyError:
        raise ValueError(f"task set {taskset_id!r} not found") from None
    for split in ("tasks", "test", "val", "train"):
        if split in paths:
            return paths[split]
    raise ValueError(f"task set {taskset_id!r} has no task files")


def _require_script(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"CLI entry point not found: {path}")


# Exec backends the studio can run tasks with, and the CLI each one shells out to.
EXEC_BACKENDS = {
    "claude_code_exec": "claude",
    "codex_exec": "codex",
}


def cli_path(backend: str) -> str | None:
    """Absolute path of the backend's CLI on PATH, or None when not installed."""
    cli = EXEC_BACKENDS.get(backend)
    return shutil.which(cli) if cli else None


def _resolve_target_backend(params: dict) -> str:
    """Validated exec backend from params (default claude_code_exec); fail-fast
    when its CLI is not installed so no job is queued that can only crash."""
    backend = str(params.get("target_backend") or "claude_code_exec")
    if backend not in EXEC_BACKENDS:
        raise ValueError(
            f"target_backend must be one of {sorted(EXEC_BACKENDS)}, got {backend!r}"
        )
    if cli_path(backend) is None:
        raise ValueError(
            f"target_backend {backend!r} requires the '{EXEC_BACKENDS[backend]}' CLI, "
            "which was not found on PATH — install it and log in first"
        )
    return backend


_SPLIT_RATIO_RE = re.compile(r"^[1-9]\d*:[1-9]\d*:[1-9]\d*$")

# key → (min, max); mirrored by the wizard forms so both sides agree
PARAM_RANGES = {
    "workers": (1, 8),
    "timeout": (60, 3600),
    "limit": (0, 10000),
    "num_epochs": (1, 10),
    "learning_rate": (1, 16),
    "max_skills_per_candidate": (1, 8),
    "count": (1, 30),
}


def _validated_int(params: dict, key: str) -> int | None:
    """Range-checked int from params (None when absent); ValueError names the field."""
    value = params.get(key)
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer, got {value!r}") from None
    low, high = PARAM_RANGES[key]
    if not low <= number <= high:
        raise ValueError(f"{key} must be between {low} and {high}, got {number}")
    return number


def _validated_float(
    params: dict,
    key: str,
    low: float,
    high: float,
) -> float | None:
    value = params.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number, got {value!r}") from None
    if not low <= number <= high:
        raise ValueError(f"{key} must be between {low} and {high}, got {number}")
    return number


def _resolve_skill_selection(config: StudioConfig, params: dict) -> list[SkillInfo]:
    raw_skill_ids = params.get("skill_ids")
    target_mode = params.get("target_mode")
    if raw_skill_ids is None:
        if target_mode not in (None, "skill"):
            raise ValueError("target_mode must be 'skill' when using skill_id")
        skill_id = params.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            raise ValueError("select a skill")
        return [_resolve_skill(config, skill_id)]

    if target_mode not in (None, "plugin"):
        raise ValueError("target_mode must be 'plugin' when using skill_ids")
    if "skill_id" in params:
        raise ValueError("use either skill_id or skill_ids, not both")
    if not isinstance(raw_skill_ids, list):
        raise ValueError("skill_ids must be a list")
    if any(not isinstance(skill_id, str) or not skill_id for skill_id in raw_skill_ids):
        raise ValueError("skill_ids must contain non-empty strings")
    skill_ids = list(dict.fromkeys(raw_skill_ids))
    if len(skill_ids) < 2:
        raise ValueError("plugin mode requires at least two distinct skills")

    skills = [_resolve_skill(config, skill_id) for skill_id in skill_ids]
    plugin_keys = {(skill.source, skill.plugin) for skill in skills}
    if len(plugin_keys) != 1 or any(skill.plugin is None for skill in skills):
        raise ValueError("plugin skills must belong to the same plugin")
    plugin_name = skills[0].plugin
    requested_plugin = params.get("plugin")
    if not isinstance(requested_plugin, str) or requested_plugin != plugin_name:
        raise ValueError(
            f"plugin must match the selected skills: expected {plugin_name!r}"
        )
    return skills


def _resolve_trainable_plugin_skills(
    params: dict,
    skills: list[SkillInfo],
) -> list[SkillInfo]:
    raw_trainable_ids = params.get("trainable_skill_ids")
    if raw_trainable_ids is None:
        trainable_ids = [item.id for item in skills]
    elif not isinstance(raw_trainable_ids, list):
        raise ValueError("trainable_skill_ids must be a list")
    elif any(
        not isinstance(skill_id, str) or not skill_id
        for skill_id in raw_trainable_ids
    ):
        raise ValueError("trainable_skill_ids must contain non-empty strings")
    else:
        trainable_ids = list(dict.fromkeys(raw_trainable_ids))
    selected_by_id = {item.id: item for item in skills}
    unknown_trainable = sorted(set(trainable_ids) - set(selected_by_id))
    if unknown_trainable:
        raise ValueError(
            f"trainable_skill_ids must be selected Plugin skills: {unknown_trainable}"
        )
    if not trainable_ids:
        raise ValueError("select at least one trainable Plugin Skill")
    return [selected_by_id[skill_id] for skill_id in trainable_ids]


def analyze_plugin_training_coverage(
    config: StudioConfig,
    params: dict,
) -> PluginCoverageReport:
    """Analyze a task set against the currently selected trainable Skills."""
    plugin_params = dict(params)
    plugin_params["target_mode"] = "plugin"
    skills = _resolve_skill_selection(config, plugin_params)
    trainable_skills = _resolve_trainable_plugin_skills(plugin_params, skills)
    plugin_state = collect_plugin_state([item.path for item in skills])
    runtime_names_by_id = dict(
        zip((item.id for item in skills), plugin_state.names, strict=True)
    )
    trainable_names = [
        runtime_names_by_id[skill.id] for skill in trainable_skills
    ]

    taskset_id = str(params.get("taskset_id", ""))
    taskset = tasksets.get_taskset(config, taskset_id)
    if taskset is None:
        raise ValueError(f"task set {taskset_id!r} not found")
    tasks_by_split = tasksets.get_taskset_tasks(config, taskset_id)
    all_items = [
        item
        for split_items in tasks_by_split.values()
        for item in split_items
    ]
    normalize_plugin_tasks(all_items, set(plugin_state.names))

    generation_minimum = minimum_plugin_task_count(len(trainable_skills))
    reasons: list[str] = []
    skill_rows: list[PluginSkillCoverage] = []
    if taskset.mode == "single":
        source_items = list(tasks_by_split.get("tasks", []))
        split_seed = int(
            load_structured_config(str(TRAIN_BASE_CONFIG))["env"].get(
                "split_seed",
                42,
            )
        )
        random.Random(split_seed).shuffle(source_items)
        counts = target_skill_counts(source_items, trainable_names)
        try:
            plan_disjoint_plugin_coverage(
                source_items,
                trainable_names,
                trainable_names,
            )
        except ValueError as exc:
            reasons.append(str(exc))
        for skill in trainable_skills:
            runtime_name = runtime_names_by_id[skill.id]
            skill_rows.append(
                PluginSkillCoverage(
                    skill_id=skill.id,
                    skill_name=runtime_name,
                    count=counts[runtime_name],
                    required=PLUGIN_MIN_TASKS_PER_SKILL,
                )
            )
    else:
        train_items = tasks_by_split.get("train", [])
        validation_items = tasks_by_split.get("val", [])
        source_counts = target_skill_counts(all_items, trainable_names)
        train_counts = target_skill_counts(train_items, trainable_names)
        validation_counts = target_skill_counts(
            validation_items,
            trainable_names,
        )
        missing_train = [
            name for name in trainable_names if train_counts[name] < 1
        ]
        missing_validation = [
            name for name in trainable_names if validation_counts[name] < 1
        ]
        if missing_train:
            reasons.append(
                "training tasks must target every trainable Plugin Skill; "
                f"missing coverage for: {missing_train}"
            )
        if missing_validation:
            reasons.append(
                "validation tasks must target every trainable Plugin Skill; "
                f"missing coverage for: {missing_validation}"
            )
        for skill in trainable_skills:
            runtime_name = runtime_names_by_id[skill.id]
            skill_rows.append(
                PluginSkillCoverage(
                    skill_id=skill.id,
                    skill_name=runtime_name,
                    count=source_counts[runtime_name],
                    required=PLUGIN_MIN_TASKS_PER_SKILL,
                    train_count=train_counts[runtime_name],
                    validation_count=validation_counts[runtime_name],
                )
            )

    return PluginCoverageReport(
        valid=not reasons,
        mode=taskset.mode,
        total_count=len(all_items),
        generation_minimum_count=generation_minimum,
        minimum_tasks_per_skill=PLUGIN_MIN_TASKS_PER_SKILL,
        skills=skill_rows,
        reasons=reasons,
    )


def build_eval_command(config: StudioConfig, params: dict, job_dir: Path) -> list[str]:
    """argv for scripts/evaluate_skill.py; output goes to <job_dir>/out."""
    _require_script(EVAL_SCRIPT)
    skills = _resolve_skill_selection(config, params)
    tasks_file = _resolve_taskset_file(config, str(params.get("taskset_id", "")))
    target_backend = _resolve_target_backend(params)

    argv = [
        PYTHON, str(EVAL_SCRIPT),
    ]
    for skill in skills:
        argv += ["--skill", skill.path]
    argv += [
        "--tasks", str(tasks_file),
        "--out_root", str(job_dir / "out"),
        "--target_backend", target_backend,
    ]
    for flag, key in (
        ("--model", "model"),
        ("--optimizer_model", "optimizer_model"),
        ("--optimizer_backend", "optimizer_backend"),
    ):
        value = params.get(key)
        if value:
            argv += [flag, str(value)]
    for flag, key in (("--workers", "workers"), ("--timeout", "timeout"), ("--limit", "limit")):
        value = _validated_int(params, key)
        if value is not None:
            argv += [flag, str(value)]
    return argv


def _materialize_taskgen_expansion(
    config: StudioConfig,
    params: dict,
    job_dir: Path,
) -> tuple[Path, str] | None:
    has_taskset = "taskset_id" in params
    has_target = "target_split" in params
    if has_taskset != has_target:
        raise ValueError("taskset_id and target_split must be provided together for taskgen expansion")
    if not has_taskset:
        return None

    taskset_id = params.get("taskset_id")
    target_split = params.get("target_split")
    if not isinstance(taskset_id, str) or not taskset_id:
        raise ValueError("taskset_id must be a non-empty string")
    if not isinstance(target_split, str) or not target_split:
        raise ValueError("target_split must be a non-empty string")
    taskset = tasksets.get_taskset(config, taskset_id)
    if taskset is None:
        raise ValueError(f"task set {taskset_id!r} not found")
    if taskset.sample:
        raise ValueError(f"task set {taskset_id!r} is a read-only sample and cannot be expanded")

    tasks_by_split = tasksets.get_taskset_tasks(config, taskset_id)
    if taskset.mode == "single":
        if target_split != "tasks":
            raise ValueError("single-mode taskgen expansion target_split must be 'tasks'")
    elif target_split not in tasks_by_split and target_split != "test":
        raise ValueError(
            f"split-mode target_split must be an existing split or optional 'test', got {target_split!r}"
        )

    job_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = job_dir / "existing_tasks.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "taskset_id": taskset_id,
                "target_split": target_split,
                "tasks_by_split": tasks_by_split,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return snapshot_path, target_split


def build_taskgen_command(config: StudioConfig, params: dict, job_dir: Path) -> list[str]:
    """argv for scripts/generate_tasks.py; output goes to <job_dir>/out."""
    _require_script(GEN_SCRIPT)
    skills = _resolve_skill_selection(config, params)
    target_backend = _resolve_target_backend(params)
    expansion = _materialize_taskgen_expansion(config, params, job_dir)

    count = _validated_int(params, "count")
    effective_count = count if count is not None else 5
    if len(skills) > 1:
        effective_count = max(
            effective_count,
            minimum_plugin_task_count(len(skills)),
        )
    argv = [
        PYTHON, str(GEN_SCRIPT),
    ]
    for skill in skills:
        argv += ["--skill", skill.path]
    argv += [
        "--backend", target_backend,
        "--count", str(effective_count),
        "--out_root", str(job_dir / "out"),
    ]
    if len(skills) > 1:
        argv += ["--min-tasks-per-skill", str(PLUGIN_MIN_TASKS_PER_SKILL)]
    if params.get("model"):
        argv += ["--model", str(params["model"])]
    if params.get("guidance"):
        argv += ["--guidance", str(params["guidance"])]
    timeout = _validated_int(params, "timeout")
    if timeout is not None:
        argv += ["--timeout", str(timeout)]
    if expansion is not None:
        snapshot_path, target_split = expansion
        argv += [
            "--existing-tasks", str(snapshot_path),
            "--target-split", target_split,
        ]
    return argv


def _materialize_split_dir(config: StudioConfig, taskset_id: str, job_dir: Path) -> tuple[Path, bool]:
    """Copy a split taskset's flat files into the train/ val/ test/ items.json
    layout SplitDataLoader requires.  Returns (split_dir, has_real_test):
    when the taskset has no test.json, val doubles as test so the loader's
    all-three-splits check passes, and eval_test should default off."""
    paths = tasksets.taskset_file_paths(config, taskset_id)
    if "train" not in paths or "val" not in paths:
        raise ValueError(f"task set {taskset_id!r} is not a split set with train/val files")
    split_root = job_dir / "split"
    has_real_test = "test" in paths
    plan = {"train": paths["train"], "val": paths["val"], "test": paths.get("test", paths["val"])}
    for split, source in plan.items():
        split_dir = split_root / split
        split_dir.mkdir(parents=True, exist_ok=True)
        (split_dir / "items.json").write_bytes(source.read_bytes())
    return split_root, has_real_test


def build_train_command(config: StudioConfig, params: dict, job_dir: Path) -> list[str]:
    """Write <job_dir>/config.yaml and return the argv (or bash pipeline when
    a bundle step must run first) for scripts/train.py."""
    plugin_mode = params.get("skill_ids") is not None
    _require_script(PLUGIN_TRAIN_SCRIPT if plugin_mode else TRAIN_SCRIPT)
    if not TRAIN_BASE_CONFIG.is_file():
        raise ValueError(f"base train config not found: {TRAIN_BASE_CONFIG}")
    skills = _resolve_skill_selection(config, params)
    skill = skills[0]
    runtime_names_by_id: dict[str, str] = {}
    if plugin_mode:
        plugin_state = collect_plugin_state([item.path for item in skills])
        runtime_names_by_id = dict(
            zip((item.id for item in skills), plugin_state.names, strict=True)
        )
    taskset_id = str(params.get("taskset_id", ""))
    taskset = tasksets.get_taskset(config, taskset_id)
    if taskset is None:
        raise ValueError(f"task set {taskset_id!r} not found")

    target_backend = _resolve_target_backend(params)
    cfg = load_structured_config(str(TRAIN_BASE_CONFIG))
    job_dir.mkdir(parents=True, exist_ok=True)
    skill_dir = Path(skill.path)

    cfg["model"]["target_backend"] = target_backend
    if params.get("target_model"):
        cfg["model"]["target"] = str(params["target_model"])
    elif target_backend == "codex_exec":
        # the YAML default target is a Claude model — swap to the backend's default
        cfg["model"]["target"] = default_model_for_backend(target_backend)
    if params.get("optimizer_model"):
        cfg["model"]["optimizer"] = str(params["optimizer_model"])
    num_epochs = _validated_int(params, "num_epochs")
    if num_epochs is not None:
        cfg["train"]["num_epochs"] = num_epochs
    learning_rate = _validated_int(params, "learning_rate")
    if learning_rate is not None:
        cfg["optimizer"]["learning_rate"] = learning_rate
    if params.get("gate_metric"):
        gate_metric = str(params["gate_metric"])
        if gate_metric not in ("hard", "soft", "mixed"):
            raise ValueError(f"gate_metric must be hard | soft | mixed, got {gate_metric!r}")
        cfg["evaluation"]["gate_metric"] = gate_metric

    trainable_files = [str(f) for f in (params.get("trainable_files") or []) if str(f).strip()]
    if plugin_mode and trainable_files:
        raise ValueError("Plugin training does not support trainable_files")
    bundle_path = job_dir / "seed_bundle.md"
    if plugin_mode:
        cfg["env"].pop("skill_init", None)
        cfg["env"].pop("skill_dir", None)
        cfg["env"].pop("trainable_files", None)
        trainable_skills = _resolve_trainable_plugin_skills(params, skills)
        max_skills = _validated_int(params, "max_skills_per_candidate")
        if max_skills is not None:
            if max_skills > len(trainable_skills):
                raise ValueError(
                    "max_skills_per_candidate cannot exceed the trainable Skill count"
                )
            cfg["optimizer"]["max_skills_per_candidate"] = max_skills
        max_regression = _validated_float(
            params,
            "max_skill_regression",
            0.0,
            1.0,
        )
        if max_regression is not None:
            cfg["evaluation"]["max_skill_regression"] = max_regression
    elif trainable_files:
        cfg["env"]["skill_init"] = str(bundle_path)
        cfg["env"]["trainable_files"] = trainable_files
    else:
        cfg["env"]["skill_init"] = str(skill_dir / "SKILL.md")
    cfg["env"]["skill_dir"] = str(skill_dir)

    if taskset.mode == "split":
        split_root, has_real_test = _materialize_split_dir(config, taskset_id, job_dir)
        cfg["env"]["split_mode"] = "split_dir"
        cfg["env"]["split_dir"] = str(split_root)
        cfg["env"].pop("data_path", None)
        if not has_real_test and params.get("eval_test") is None:
            cfg["evaluation"]["eval_test"] = False  # test/ is a copy of val — don't score it
    else:
        tasks_file = _resolve_taskset_file(config, taskset_id)
        split_ratio = str(params.get("split_ratio") or "4:3:3")
        if not _SPLIT_RATIO_RE.match(split_ratio):
            raise ValueError(f"split_ratio must look like '4:3:3', got {split_ratio!r}")
        cfg["env"]["split_mode"] = "ratio"
        cfg["env"]["data_path"] = str(tasks_file)
        cfg["env"]["split_ratio"] = split_ratio
        cfg["env"]["split_output_dir"] = str(job_dir / "split")
        cfg["env"].pop("split_dir", None)
    if params.get("eval_test") is not None:
        cfg["evaluation"]["eval_test"] = bool(params["eval_test"])
    if plugin_mode:
        coverage_report = analyze_plugin_training_coverage(config, params)
        if not coverage_report.valid:
            raise ValueError("; ".join(coverage_report.reasons))
    for cfg_key in ("workers", "timeout", "limit"):
        value = _validated_int(params, cfg_key)
        if value is not None:
            cfg["env"][cfg_key] = value

    config_path = job_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    train_argv = [PYTHON, str(PLUGIN_TRAIN_SCRIPT if plugin_mode else TRAIN_SCRIPT)]
    if plugin_mode:
        for selected in skills:
            train_argv += ["--skill", selected.path]
        for trainable in trainable_skills:
            train_argv += ["--train-skill", runtime_names_by_id[trainable.id]]
    train_argv += [
        "--config", str(config_path),
        "--out_root", str(job_dir / "out"),
    ]
    if not trainable_files:
        return train_argv

    bundle_argv = [
        PYTHON, "-m", BUNDLE_MODULE, "build", str(skill_dir),
        "--files", ",".join(trainable_files),
        "--out", str(bundle_path),
    ]
    # Two sequential steps in one process tree; echo markers make it obvious
    # in log.txt whether a failure came from the bundle step or the train step.
    script = (
        "set -e\n"
        "echo '[studio] step 1/2: bundle build'\n"
        f"{shlex.join(bundle_argv)}\n"
        "echo '[studio] step 2/2: train'\n"
        f"{shlex.join(train_argv)}\n"
    )
    return ["bash", "-c", script]
