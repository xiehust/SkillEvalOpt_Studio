"""Tests for skillopt_studio runners/artifacts/dashboard with stub CLIs.

No model calls: eval/train "runs" are fake scripts (monkeypatched in place of
scripts/evaluate_skill.py and scripts/train.py) that write artifacts with the
same on-disk layout as the real CLIs, in well under a second.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from skillopt_studio import artifacts, runners
from skillopt_studio.app import create_app
from skillopt_studio.config import StudioConfig
from skillopt_studio.jobs import JobManager
from skillopt_studio.models import JobInfo
from skillopt_studio.runners import build_eval_command, build_taskgen_command, build_train_command
from skillopt_studio.skill_sources import scan_skills, upload_skill_zip
from skillopt_studio.tasksets import save_taskset

from tests.test_studio_core import (
    SOURCES,
    make_plugins_root,
    make_skill,
    make_zip,
    valid_tasks,
    wait_until,
)

# ── stub CLIs (same artifact layout as the real scripts) ─────────────────

FAKE_EVAL_SCRIPT = """\
import argparse, json, os
p = argparse.ArgumentParser()
for flag in ("--tasks", "--out_root", "--model", "--optimizer_model",
             "--target_backend", "--optimizer_backend"):
    p.add_argument(flag, default="")
p.add_argument("--skill", action="append", default=[])
for flag in ("--workers", "--timeout", "--limit"):
    p.add_argument(flag, type=int, default=0)
a = p.parse_args()
os.makedirs(a.out_root, exist_ok=True)
with open(a.tasks, encoding="utf-8") as f:
    tasks = json.load(f)
print(f"[skilleval] tasks: {len(tasks)} from {a.tasks}", flush=True)
results = [
    {"id": t["id"], "task_type": t.get("task_type", "default"),
     "target_skills": t.get("target_skills", []),
     "hard": 1 if i % 2 == 0 else 0, "soft": 0.75 if i % 2 == 0 else 0.25,
     "judge_reason": "stub verdict", "duration_s": 1.5}
    for i, t in enumerate(tasks)
]
def skill_name(path):
    skill_file = os.path.join(path, "SKILL.md") if os.path.isdir(path) else path
    with open(skill_file, encoding="utf-8") as f:
        for line in f:
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip().strip("'\\\"")
    return os.path.basename(os.path.dirname(path) if os.path.isfile(path) else path)
def metrics(rows):
    return {
        "count": len(rows),
        "hard": sum(float(row["hard"]) for row in rows) / len(rows) if rows else 0.0,
        "soft": sum(float(row["soft"]) for row in rows) / len(rows) if rows else 0.0,
    }
skill_names = [skill_name(path) for path in a.skill]
by_skill = {
    name: metrics([row for row in results if name in row["target_skills"]])
    for name in skill_names
}
by_type = {
    task_type: metrics([row for row in results if row["task_type"] == task_type])
    for task_type in sorted({row["task_type"] for row in results})
}
routing = [row for row in results if row["task_type"] == "routing"]
integration = [
    row for row in results
    if row["task_type"] == "integration" or len(row["target_skills"]) > 1
]
covered = [(name, metric) for name, metric in by_skill.items() if metric["count"]]
weakest = min(covered, key=lambda entry: (entry[1]["hard"], entry[1]["soft"], entry[0])) if covered else None
summary = {
    "mode": "plugin" if len(skill_names) > 1 else "skill",
    "skill_count": len(skill_names),
    "skill_names": skill_names,
    "overall": metrics(results),
    "by_skill": by_skill,
    "by_task_type": by_type,
    "routing": metrics(routing) if routing else None,
    "integration": metrics(integration) if integration else None,
    "weakest_skill": ({"name": weakest[0], **weakest[1]} if weakest else None),
}
print(f"[skilleval] judging {len(results)} responses", flush=True)
with open(os.path.join(a.out_root, "results.json"), "w", encoding="utf-8") as f:
    json.dump(results, f)
with open(os.path.join(a.out_root, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f)
with open(os.path.join(a.out_root, "report.md"), "w", encoding="utf-8") as f:
    f.write("# Skill Evaluation Report (stub)")
"""

FAKE_TRAIN_SCRIPT = """\
import argparse, json, os, yaml
p = argparse.ArgumentParser()
p.add_argument("--config")
p.add_argument("--out_root")
a = p.parse_args()
with open(a.config, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
out = a.out_root
os.makedirs(os.path.join(out, "skills"), exist_ok=True)
with open(cfg["env"]["skill_init"], encoding="utf-8") as f:
    seed = f.read()
with open(os.path.join(out, "skills", "skill_v0000.md"), "w", encoding="utf-8") as f:
    f.write(seed)
best = seed + "\\n## Learned rule\\nAlways cite the rubric.\\n"
with open(os.path.join(out, "best_skill.md"), "w", encoding="utf-8") as f:
    f.write(best)
history = [
    {"step": 1, "epoch": 1, "action": "accept", "selection_hard": 0.5, "selection_soft": 0.6,
     "current_score": 0.5, "best_score": 0.5, "best_step": 1, "skill_len": len(seed),
     "wall_time_s": 2.0},
    {"step": 2, "epoch": 1, "action": "reject", "selection_hard": 0.4, "selection_soft": 0.5,
     "current_score": 0.5, "best_score": 0.5, "best_step": 1, "skill_len": len(seed),
     "wall_time_s": 1.5},
]
with open(os.path.join(out, "history.json"), "w", encoding="utf-8") as f:
    json.dump(history, f)
for rec in history:
    step_dir = os.path.join(out, "steps", f"step_{rec['step']:04d}")
    os.makedirs(step_dir, exist_ok=True)
    with open(os.path.join(step_dir, "step_record.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)
summary = {
    "best_selection_hard": 0.5, "baseline_selection_hard": 0.25, "best_step": 1,
    "total_steps": 2, "total_accepts": 1, "total_rejects": 1, "total_skips": 0,
    "baseline_test_hard": 0.2, "test_hard": 0.6, "final_test_hard": 0.55,
    "total_wall_time_s": 3.5,
    "token_summary": {"_total": {"total_tokens": 1234, "prompt_tokens": 1000,
                                 "completion_tokens": 234, "calls": 7}},
}
with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f)
with open(os.path.join(out, "config_echo.json"), "w", encoding="utf-8") as f:
    json.dump(cfg, f)
print("  [STEP 2 done] epoch=1 action=reject current=0.5 best=0.5", flush=True)
"""

FAKE_PLUGIN_TRAIN_SCRIPT = """\
import argparse, json, os, shutil, yaml
p = argparse.ArgumentParser()
p.add_argument("--config")
p.add_argument("--out_root")
p.add_argument("--skill", action="append", default=[])
p.add_argument("--train-skill", action="append", default=[])
a = p.parse_args()
with open(a.config, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
out = a.out_root
os.makedirs(out, exist_ok=True)
def skill_name(path):
    with open(os.path.join(path, "SKILL.md"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip().strip("'\\\"")
    return os.path.basename(path)
names = [skill_name(path) for path in a.skill]
for root_name in ("plugin_versions/plugin_v0000", "best_plugin"):
    root = os.path.join(out, root_name)
    os.makedirs(root, exist_ok=True)
    for path, name in zip(a.skill, names):
        destination = os.path.join(root, name)
        shutil.copytree(path, destination, dirs_exist_ok=True)
        if root_name == "best_plugin" and name in a.train_skill:
            with open(os.path.join(destination, "SKILL.md"), "a", encoding="utf-8") as f:
                f.write("\\n## Learned Plugin rule\\n")
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "skill_names": names,
                   "trainable_skill_names": a.train_skill}, f)
history = [{
    "step": 1, "epoch": 1, "action": "accept_new_best",
    "selected_skills": a.train_skill[:2],
    "attribution_counts": {"execution": 2},
    "current_score": 0.75, "best_score": 0.75, "best_step": 1,
    "current_skill_scores": {name: 0.75 for name in names},
    "regressions": {name: 0.0 for name in names}, "wall_time_s": 1.0,
}]
with open(os.path.join(out, "history.json"), "w", encoding="utf-8") as f:
    json.dump(history, f)
summary = {
    "mode": "plugin", "skill_names": names,
    "trainable_skill_names": a.train_skill,
    "baseline_aggregates": {
        "overall": {"count": 2, "hard": 0.5, "soft": 0.5},
        "by_skill": {name: {"count": 1, "hard": 0.5, "soft": 0.5} for name in names},
    },
    "best_aggregates": {
        "overall": {"count": 2, "hard": 0.75, "soft": 0.8},
        "by_skill": {name: {"count": 1, "hard": 0.75, "soft": 0.8} for name in names},
    },
    "test_aggregates": None,
    "best_selection_score": 0.75,
    "best_skill_scores": {name: 0.75 for name in names},
    "best_step": 1, "total_steps": 1, "total_accepts": 1,
    "total_rejects": 0, "total_skips": 0,
    "token_summary": {"_total": {"total_tokens": 321, "prompt_tokens": 250,
                                 "completion_tokens": 71, "calls": 3}},
}
with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f)
with open(os.path.join(out, "config_echo.json"), "w", encoding="utf-8") as f:
    json.dump(cfg, f)
print("  [PLUGIN STEP 1/1] action=accept_new_best score=0.7500", flush=True)
"""


FAKE_GEN_SCRIPT = """\
import argparse, json, os
p = argparse.ArgumentParser()
for flag in ("--backend", "--model", "--guidance", "--out_root"):
    p.add_argument(flag, default="")
p.add_argument("--skill", action="append", default=[])
for flag in ("--count", "--timeout"):
    p.add_argument(flag, type=int, default=0)
a = p.parse_args()
os.makedirs(a.out_root, exist_ok=True)
tasks = [
    {"id": f"gen_{i:03d}", "question": f"Generated Q{i}?", "rubric": f"Must satisfy criterion {i}.",
     "task_type": "generated"}
    for i in range(a.count)
]
print(f"[taskgen] attempt 1/2", flush=True)
with open(os.path.join(a.out_root, "generated_tasks.json"), "w", encoding="utf-8") as f:
    json.dump(tasks, f)
summary = {"count": len(tasks), "requested_count": a.count, "backend": a.backend,
           "model": a.model, "skill": a.skill[0] if len(a.skill) == 1 else None,
           "skills": a.skill, "skill_count": len(a.skill), "attempts": 1, "duration_s": 0.1}
with open(os.path.join(a.out_root, "gen_summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f)
print(f"[taskgen] done: {len(tasks)} tasks", flush=True)
"""


@pytest.fixture
def studio_config(tmp_path: Path) -> StudioConfig:
    return StudioConfig(
        studio_root=tmp_path / "studio",
        skill_sources={name: tmp_path / "sources" / name for name in SOURCES},
    )


@pytest.fixture
def stub_scripts(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    eval_path = tmp_path / "stub_evaluate_skill.py"
    eval_path.write_text(FAKE_EVAL_SCRIPT, encoding="utf-8")
    train_path = tmp_path / "stub_train.py"
    train_path.write_text(FAKE_TRAIN_SCRIPT, encoding="utf-8")
    plugin_train_path = tmp_path / "stub_train_plugin.py"
    plugin_train_path.write_text(FAKE_PLUGIN_TRAIN_SCRIPT, encoding="utf-8")
    gen_path = tmp_path / "stub_generate_tasks.py"
    gen_path.write_text(FAKE_GEN_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(runners, "EVAL_SCRIPT", eval_path)
    monkeypatch.setattr(runners, "TRAIN_SCRIPT", train_path)
    monkeypatch.setattr(runners, "PLUGIN_TRAIN_SCRIPT", plugin_train_path)
    monkeypatch.setattr(runners, "GEN_SCRIPT", gen_path)
    # hermetic: pretend both exec CLIs are installed regardless of the host
    monkeypatch.setattr(
        runners, "cli_path", lambda backend: f"/stub/bin/{runners.EXEC_BACKENDS[backend]}"
    )
    return {
        "eval": eval_path,
        "train": train_path,
        "plugin_train": plugin_train_path,
        "taskgen": gen_path,
    }


@pytest.fixture
def claude_skill(studio_config) -> Path:
    return make_skill(
        studio_config.skill_sources["claude"], "local-skill",
        "---\ndescription: a local skill\n---\n# Local\nUse the checklist.\n",
    )


@pytest.fixture
def plugin_skills(studio_config, tmp_path) -> tuple[Path, Path]:
    root = make_plugins_root(tmp_path, {})
    install = root / "cache" / "market" / "knowledge" / "1.0.0"
    skill_a = make_skill(
        install / "skills",
        "init",
        "---\nname: knowledge-init\ndescription: Initialize knowledge\n---\n# Init\n",
    )
    skill_b = make_skill(
        install / "skills",
        "status",
        "---\nname: knowledge-status\ndescription: Show knowledge status\n---\n# Status\n",
    )
    make_plugins_root(
        tmp_path,
        {"knowledge@market": [{"scope": "user", "installPath": str(install)}]},
    )
    studio_config.skill_sources["claude-plugins"] = root
    return skill_a, skill_b


@pytest.fixture
def single_taskset(studio_config):
    return save_taskset(studio_config, "single-set", {"tasks": valid_tasks(count=4)}, "single")


@pytest.fixture
def split_taskset(studio_config):
    files = {"train": valid_tasks("tr", 4), "val": valid_tasks("va", 2), "test": valid_tasks("te", 2)}
    return save_taskset(studio_config, "split-set", files, "split")


def job_dir_of(studio_config: StudioConfig, name: str = "job-x") -> Path:
    path = studio_config.jobs_dir / name
    path.mkdir(parents=True, exist_ok=True)
    return path


class TestBuildEvalCommand:
    def test_claude_source_skill_argv(self, studio_config, stub_scripts, claude_skill, single_taskset):
        job_dir = job_dir_of(studio_config)
        argv = build_eval_command(
            studio_config,
            {"skill_id": "claude--local-skill", "taskset_id": "single-set",
             "model": "claude-sonnet-4-6", "optimizer_model": "openai.gpt-5.5",
             "workers": 2, "timeout": 300},
            job_dir,
        )
        assert argv[0] == runners.PYTHON
        assert argv[1] == str(stub_scripts["eval"])
        assert argv[argv.index("--skill") + 1] == str(claude_skill)
        assert argv[argv.index("--tasks") + 1].endswith("tasksets/single-set/tasks.json")
        assert argv[argv.index("--out_root") + 1] == str(job_dir / "out")
        assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
        assert argv[argv.index("--optimizer_model") + 1] == "openai.gpt-5.5"
        assert argv[argv.index("--workers") + 1] == "2"
        assert argv[argv.index("--timeout") + 1] == "300"

    def test_uploaded_skill_argv(self, studio_config, stub_scripts, single_taskset):
        info = upload_skill_zip(studio_config, make_zip({"SKILL.md": "# up\n"}), "up-skill")
        argv = build_eval_command(
            studio_config,
            {"skill_id": info.id, "taskset_id": "single-set"},
            job_dir_of(studio_config),
        )
        assert argv[argv.index("--skill") + 1] == str(studio_config.skills_dir / "up-skill")

    def test_plugin_skills_emit_repeated_flags(
        self, studio_config, stub_scripts, plugin_skills, single_taskset
    ):
        plugin_infos = [skill for skill in scan_skills(studio_config) if skill.plugin == "knowledge"]
        ids = [skill.id for skill in plugin_infos]
        argv = build_eval_command(
            studio_config,
            {
                "target_mode": "plugin",
                "skill_ids": ids,
                "plugin": "knowledge",
                "taskset_id": "single-set",
            },
            job_dir_of(studio_config),
        )
        assert [argv[index + 1] for index, value in enumerate(argv) if value == "--skill"] == [
            str(skill.path) for skill in plugin_infos
        ]

    def test_plugin_selection_rejects_cross_plugin_or_spoofed_name(
        self,
        studio_config,
        stub_scripts,
        plugin_skills,
        claude_skill,
        single_taskset,
    ):
        with pytest.raises(ValueError, match="same plugin"):
            build_eval_command(
                studio_config,
                {
                    "skill_ids": [
                        "claude-plugins--knowledge-init",
                        "claude--local-skill",
                    ],
                    "plugin": "knowledge",
                    "taskset_id": "single-set",
                },
                job_dir_of(studio_config, "eval-cross-plugin"),
            )
        with pytest.raises(ValueError, match="expected 'knowledge'"):
            build_eval_command(
                studio_config,
                {
                    "skill_ids": [
                        "claude-plugins--knowledge-init",
                        "claude-plugins--knowledge-status",
                    ],
                    "plugin": "spoofed",
                    "taskset_id": "single-set",
                },
                job_dir_of(studio_config, "eval-spoofed-plugin"),
            )

    def test_plugin_selection_requires_distinct_ids_and_no_scalar_mix(
        self, studio_config, stub_scripts, plugin_skills, single_taskset
    ):
        with pytest.raises(ValueError, match="at least two distinct"):
            build_eval_command(
                studio_config,
                {
                    "skill_ids": [
                        "claude-plugins--knowledge-init",
                        "claude-plugins--knowledge-init",
                    ],
                    "plugin": "knowledge",
                    "taskset_id": "single-set",
                },
                job_dir_of(studio_config, "eval-duplicate-plugin"),
            )
        with pytest.raises(ValueError, match="either skill_id or skill_ids"):
            build_eval_command(
                studio_config,
                {
                    "skill_id": "claude-plugins--knowledge-init",
                    "skill_ids": [
                        "claude-plugins--knowledge-init",
                        "claude-plugins--knowledge-status",
                    ],
                    "plugin": "knowledge",
                    "taskset_id": "single-set",
                },
                job_dir_of(studio_config, "eval-mixed-contract"),
            )

    def test_target_mode_must_match_selection_shape(
        self, studio_config, stub_scripts, plugin_skills, single_taskset
    ):
        with pytest.raises(ValueError, match="when using skill_ids"):
            build_eval_command(
                studio_config,
                {
                    "target_mode": "skill",
                    "skill_ids": [
                        "claude-plugins--knowledge-init",
                        "claude-plugins--knowledge-status",
                    ],
                    "plugin": "knowledge",
                    "taskset_id": "single-set",
                },
                job_dir_of(studio_config, "eval-wrong-mode"),
            )

    def test_unknown_skill_fails_fast(self, studio_config, stub_scripts, single_taskset):
        with pytest.raises(ValueError, match="skill 'claude--nope' not found"):
            build_eval_command(
                studio_config,
                {"skill_id": "claude--nope", "taskset_id": "single-set"},
                job_dir_of(studio_config),
            )

    def test_unknown_taskset_fails_fast(self, studio_config, stub_scripts, claude_skill):
        with pytest.raises(ValueError, match="task set 'nope' not found"):
            build_eval_command(
                studio_config,
                {"skill_id": "claude--local-skill", "taskset_id": "nope"},
                job_dir_of(studio_config),
            )

    def test_missing_cli_script_fails_fast(self, studio_config, monkeypatch, claude_skill, single_taskset):
        monkeypatch.setattr(runners, "EVAL_SCRIPT", Path("/nonexistent/evaluate_skill.py"))
        with pytest.raises(ValueError, match="CLI entry point not found"):
            build_eval_command(
                studio_config,
                {"skill_id": "claude--local-skill", "taskset_id": "single-set"},
                job_dir_of(studio_config),
            )


class TestBuildTrainConfig:
    def test_ratio_branch_config(self, studio_config, stub_scripts, claude_skill, single_taskset):
        job_dir = job_dir_of(studio_config, "train-ratio")
        argv = build_train_command(
            studio_config,
            {"skill_id": "claude--local-skill", "taskset_id": "single-set",
             "target_model": "claude-sonnet-4-6", "optimizer_model": "openai.gpt-5.5",
             "num_epochs": 1, "gate_metric": "soft", "eval_test": False, "learning_rate": 3},
            job_dir,
        )
        assert argv[:2] == [runners.PYTHON, str(stub_scripts["train"])]
        assert argv[argv.index("--config") + 1] == str(job_dir / "config.yaml")
        assert argv[argv.index("--out_root") + 1] == str(job_dir / "out")

        cfg = yaml.safe_load((job_dir / "config.yaml").read_text(encoding="utf-8"))
        assert "_base_" not in cfg
        assert cfg["env"]["name"] == "skilleval"
        assert cfg["env"]["split_mode"] == "ratio"
        assert cfg["env"]["data_path"].endswith("tasksets/single-set/tasks.json")
        assert cfg["env"]["split_ratio"] == "4:3:3"
        assert cfg["env"]["split_output_dir"] == str(job_dir / "split")
        assert cfg["env"]["skill_init"] == str(claude_skill / "SKILL.md")
        assert cfg["env"]["skill_dir"] == str(claude_skill)
        assert cfg["train"]["num_epochs"] == 1
        assert cfg["optimizer"]["learning_rate"] == 3
        assert cfg["evaluation"]["gate_metric"] == "soft"
        assert cfg["evaluation"]["eval_test"] is False
        assert cfg["model"]["target"] == "claude-sonnet-4-6"
        assert cfg["model"]["target_backend"] == "claude_code_exec"
        assert cfg["model"]["optimizer"] == "openai.gpt-5.5"

    def test_split_branch_materializes_split_dir(self, studio_config, stub_scripts, claude_skill, split_taskset):
        job_dir = job_dir_of(studio_config, "train-split")
        build_train_command(
            studio_config,
            {"skill_id": "claude--local-skill", "taskset_id": "split-set"},
            job_dir,
        )
        cfg = yaml.safe_load((job_dir / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["env"]["split_mode"] == "split_dir"
        assert cfg["env"]["split_dir"] == str(job_dir / "split")
        assert "data_path" not in cfg["env"]
        for split, expected in (("train", 4), ("val", 2), ("test", 2)):
            items = json.loads((job_dir / "split" / split / "items.json").read_text(encoding="utf-8"))
            assert len(items) == expected

    def test_split_without_test_falls_back_to_val(self, studio_config, stub_scripts, claude_skill):
        save_taskset(
            studio_config, "no-test",
            {"train": valid_tasks("tr", 3), "val": valid_tasks("va", 2)}, "split",
        )
        job_dir = job_dir_of(studio_config, "train-notest")
        build_train_command(
            studio_config,
            {"skill_id": "claude--local-skill", "taskset_id": "no-test"},
            job_dir,
        )
        cfg = yaml.safe_load((job_dir / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["evaluation"]["eval_test"] is False
        val_items = json.loads((job_dir / "split" / "val" / "items.json").read_text(encoding="utf-8"))
        test_items = json.loads((job_dir / "split" / "test" / "items.json").read_text(encoding="utf-8"))
        assert test_items == val_items

    def test_trainable_files_bundle_step_before_train(self, studio_config, stub_scripts, single_taskset):
        skill_dir = make_skill(studio_config.skill_sources["agents"], "multi-doc")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "rules.md").write_text("# rules\n", encoding="utf-8")
        job_dir = job_dir_of(studio_config, "train-bundle")

        argv = build_train_command(
            studio_config,
            {"skill_id": "agents--multi-doc", "taskset_id": "single-set",
             "trainable_files": ["references/rules.md"]},
            job_dir,
        )
        assert argv[:2] == ["bash", "-c"]
        script = argv[2]
        bundle_pos = script.index("skillopt.envs.skilleval.bundle")
        train_pos = script.index(str(stub_scripts["train"]))
        assert bundle_pos < train_pos, "bundle step must run before train step"
        assert "--files references/rules.md" in script
        assert "set -e" in script

        cfg = yaml.safe_load((job_dir / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["env"]["skill_init"] == str(job_dir / "seed_bundle.md")
        assert cfg["env"]["trainable_files"] == ["references/rules.md"]
        assert cfg["env"]["skill_dir"] == str(skill_dir)

    def test_plugin_training_command_and_config(
        self,
        studio_config,
        stub_scripts,
        plugin_skills,
        split_taskset,
    ):
        job_dir = job_dir_of(studio_config, "train-plugin")
        (plugin_skills[0] / ".studio_sample.json").write_text(
            json.dumps({"name": "Display-only init name"}),
            encoding="utf-8",
        )
        skill_ids = [
            "claude-plugins--knowledge-init",
            "claude-plugins--knowledge-status",
        ]
        argv = build_train_command(
            studio_config,
            {
                "target_mode": "plugin",
                "skill_ids": skill_ids,
                "trainable_skill_ids": [skill_ids[0]],
                "plugin": "knowledge",
                "taskset_id": "split-set",
                "max_skills_per_candidate": 1,
                "max_skill_regression": 0.05,
            },
            job_dir,
        )
        assert argv[:2] == [runners.PYTHON, str(stub_scripts["plugin_train"])]
        assert [argv[index + 1] for index, value in enumerate(argv) if value == "--skill"] == [
            str(path) for path in plugin_skills
        ]
        assert [
            argv[index + 1]
            for index, value in enumerate(argv)
            if value == "--train-skill"
        ] == ["knowledge-init"]
        cfg = yaml.safe_load((job_dir / "config.yaml").read_text(encoding="utf-8"))
        assert "skill_init" not in cfg["env"]
        assert cfg["optimizer"]["max_skills_per_candidate"] == 1
        assert cfg["evaluation"]["max_skill_regression"] == 0.05

    def test_plugin_training_rejects_trainable_skill_outside_selection(
        self,
        studio_config,
        stub_scripts,
        plugin_skills,
        split_taskset,
    ):
        with pytest.raises(ValueError, match="trainable_skill_ids"):
            build_train_command(
                studio_config,
                {
                    "skill_ids": [
                        "claude-plugins--knowledge-init",
                        "claude-plugins--knowledge-status",
                    ],
                    "trainable_skill_ids": ["claude-plugins--ghost"],
                    "plugin": "knowledge",
                    "taskset_id": "split-set",
                },
                job_dir_of(studio_config, "train-plugin-invalid"),
            )


class TestBuildTaskgenCommand:
    def test_argv_full_params(self, studio_config, stub_scripts, claude_skill):
        job_dir = job_dir_of(studio_config, "gen-full")
        argv = build_taskgen_command(
            studio_config,
            {"skill_id": "claude--local-skill", "target_backend": "codex_exec",
             "count": 7, "model": "gpt-5.5", "guidance": "侧重边界场景", "timeout": 600},
            job_dir,
        )
        assert argv[:2] == [runners.PYTHON, str(stub_scripts["taskgen"])]
        assert argv[argv.index("--skill") + 1] == str(claude_skill)
        assert argv[argv.index("--backend") + 1] == "codex_exec"
        assert argv[argv.index("--count") + 1] == "7"
        assert argv[argv.index("--out_root") + 1] == str(job_dir / "out")
        assert argv[argv.index("--model") + 1] == "gpt-5.5"
        assert argv[argv.index("--guidance") + 1] == "侧重边界场景"
        assert argv[argv.index("--timeout") + 1] == "600"

    def test_defaults_count_5_no_model_flag(self, studio_config, stub_scripts, claude_skill):
        argv = build_taskgen_command(
            studio_config, {"skill_id": "claude--local-skill"}, job_dir_of(studio_config, "gen-min")
        )
        assert argv[argv.index("--backend") + 1] == "claude_code_exec"
        assert argv[argv.index("--count") + 1] == "5"
        assert "--model" not in argv
        assert "--guidance" not in argv

    def test_count_out_of_range(self, studio_config, stub_scripts, claude_skill):
        with pytest.raises(ValueError, match="count must be between 1 and 30"):
            build_taskgen_command(
                studio_config,
                {"skill_id": "claude--local-skill", "count": 31},
                job_dir_of(studio_config, "gen-range"),
            )

    def test_unknown_backend_and_missing_cli(self, studio_config, stub_scripts, claude_skill, monkeypatch):
        with pytest.raises(ValueError, match="target_backend must be one of"):
            build_taskgen_command(
                studio_config,
                {"skill_id": "claude--local-skill", "target_backend": "bogus_exec"},
                job_dir_of(studio_config, "gen-bogus"),
            )
        monkeypatch.setattr(runners, "cli_path", lambda backend: None)
        with pytest.raises(ValueError, match="requires the 'claude' CLI"):
            build_taskgen_command(
                studio_config, {"skill_id": "claude--local-skill"}, job_dir_of(studio_config, "gen-nocli")
            )

    def test_unknown_skill_fails_fast(self, studio_config, stub_scripts):
        with pytest.raises(ValueError, match="skill 'claude--ghost' not found"):
            build_taskgen_command(
                studio_config, {"skill_id": "claude--ghost"}, job_dir_of(studio_config, "gen-ghost")
            )

    def test_multi_skill_argv(self, studio_config, stub_scripts, plugin_skills):
        skill_a, skill_b = plugin_skills
        argv = build_taskgen_command(
            studio_config,
            {
                "skill_ids": [
                    "claude-plugins--knowledge-init",
                    "claude-plugins--knowledge-status",
                ],
                "plugin": "knowledge",
                "count": 8,
            },
            job_dir_of(studio_config, "gen-plugin"),
        )
        skill_args = [
            argv[index + 1]
            for index, value in enumerate(argv)
            if value == "--skill"
        ]
        assert skill_args == [str(skill_a), str(skill_b)]
        assert argv[argv.index("--count") + 1] == "8"

    def test_multi_skill_requires_same_plugin(
        self, studio_config, stub_scripts, plugin_skills, claude_skill
    ):
        with pytest.raises(ValueError, match="same plugin"):
            build_taskgen_command(
                studio_config,
                {
                    "skill_ids": [
                        "claude-plugins--knowledge-init",
                        "claude--local-skill",
                    ],
                    "plugin": "knowledge",
                },
                job_dir_of(studio_config, "gen-mixed"),
            )

    def test_skill_ids_must_be_list(self, studio_config, stub_scripts):
        with pytest.raises(ValueError, match="skill_ids must be a list"):
            build_taskgen_command(
                studio_config,
                {"skill_ids": "claude--local-skill"},
                job_dir_of(studio_config, "gen-bad-list"),
            )


class TestStudioApiE2E:
    @pytest.fixture
    def client(self, studio_config, stub_scripts):
        app = create_app(studio_config)
        with TestClient(app) as test_client:
            yield test_client

    def _wait_status(self, client, job_id, status, timeout=20.0):
        assert wait_until(
            lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == status, timeout=timeout
        ), f"job {job_id} never reached {status}: {client.get(f'/api/jobs/{job_id}').json()}"

    def test_eval_job_end_to_end(self, studio_config, client, claude_skill, single_taskset):
        response = client.post(
            "/api/jobs",
            json={"type": "eval",
                  "params": {"skill_id": "claude--local-skill", "taskset_id": "single-set"}},
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        self._wait_status(client, job_id, "succeeded")

        results = client.get(f"/api/jobs/{job_id}/results")
        assert results.status_code == 200
        body = results.json()
        assert body["type"] == "eval"
        assert body["summary"]["tasks"] == 4
        assert body["summary"]["pass_rate"] == 0.5  # stub: every other task passes
        assert body["summary"]["soft_mean"] == 0.5
        assert [row["id"] for row in body["rows"]] == ["t0", "t1", "t2", "t3"]
        assert body["rows"][0]["judge_reason"] == "stub verdict"

    def test_plugin_eval_job_end_to_end(
        self,
        studio_config,
        client,
        plugin_skills,
    ):
        tasks = [
            {
                "id": "route",
                "question": "Choose the correct workflow.",
                "rubric": "Uses the initialization workflow.",
                "task_type": "routing",
                "target_skills": ["knowledge-init"],
            },
            {
                "id": "integrate",
                "question": "Initialize and report status.",
                "rubric": "Completes both workflows.",
                "task_type": "integration",
                "target_skills": ["knowledge-init", "knowledge-status"],
            },
            {
                "id": "general",
                "question": "Summarize the plugin.",
                "rubric": "Provides a concise summary.",
            },
        ]
        save_taskset(
            studio_config,
            "plugin-eval",
            {"tasks": json.dumps(tasks).encode("utf-8")},
            "single",
        )
        skill_ids = [
            "claude-plugins--knowledge-init",
            "claude-plugins--knowledge-status",
        ]
        response = client.post(
            "/api/jobs",
            json={
                "type": "eval",
                "params": {
                    "target_mode": "plugin",
                    "skill_ids": skill_ids,
                    "plugin": "knowledge",
                    "taskset_id": "plugin-eval",
                },
            },
        )
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["params"]["target_mode"] == "plugin"
        assert job["params"]["skill_ids"] == skill_ids
        assert job["params"]["plugin"] == "knowledge"
        self._wait_status(client, job["id"], "succeeded")

        body = client.get(f"/api/jobs/{job['id']}/results").json()
        aggregates = body["aggregates"]
        assert aggregates["mode"] == "plugin"
        assert aggregates["skill_count"] == 2
        assert aggregates["overall"]["count"] == 3
        assert aggregates["by_skill"]["knowledge-init"]["count"] == 2
        assert aggregates["by_skill"]["knowledge-status"]["count"] == 1
        assert aggregates["routing"]["count"] == 1
        assert aggregates["integration"]["count"] == 1
        assert aggregates["weakest_skill"]["name"] == "knowledge-status"
        assert body["rows"][0]["target_skills"] == ["knowledge-init"]

    def test_train_job_end_to_end(self, studio_config, client, claude_skill, single_taskset):
        response = client.post(
            "/api/jobs",
            json={"type": "train",
                  "params": {"skill_id": "claude--local-skill", "taskset_id": "single-set",
                             "num_epochs": 1}},
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        self._wait_status(client, job_id, "succeeded")

        body = client.get(f"/api/jobs/{job_id}/results").json()
        assert body["type"] == "train"
        steps = body["summary"]["steps"]
        assert [(s["step"], s["action"]) for s in steps] == [(1, "accept"), (2, "reject")]
        assert steps[0]["selection_hard"] == 0.5
        assert body["summary"]["best_step"] == 1
        assert body["summary"]["test_scores"] == {"baseline": 0.2, "best": 0.6, "final": 0.55}
        assert body["summary"]["token_totals"]["total_tokens"] == 1234
        assert "+## Learned rule" in body["skill_diff"]
        assert body["plugin_diffs"] == {}

        # the stub echoes the config it received — proves the generated YAML reached the CLI
        echo = client.get(f"/api/jobs/{job_id}/artifacts", params={"path": "config_echo.json"}).json()
        assert echo["kind"] == "text"
        assert json.loads(echo["content"])["env"]["name"] == "skilleval"

    def test_plugin_train_job_end_to_end(
        self,
        studio_config,
        client,
        plugin_skills,
        split_taskset,
    ):
        skill_ids = [
            "claude-plugins--knowledge-init",
            "claude-plugins--knowledge-status",
        ]
        response = client.post(
            "/api/jobs",
            json={
                "type": "train",
                "params": {
                    "target_mode": "plugin",
                    "plugin": "knowledge",
                    "skill_ids": skill_ids,
                    "trainable_skill_ids": [skill_ids[0]],
                    "taskset_id": "split-set",
                    "num_epochs": 1,
                },
            },
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        self._wait_status(client, job_id, "succeeded")

        body = client.get(f"/api/jobs/{job_id}/results").json()
        assert body["summary"]["mode"] == "plugin"
        assert body["summary"]["plugin"]["skill_names"] == [
            "knowledge-init",
            "knowledge-status",
        ]
        assert body["summary"]["plugin"]["trainable_skill_names"] == [
            "knowledge-init"
        ]
        assert body["summary"]["steps"][0]["selected_skills"] == [
            "knowledge-init"
        ]
        assert "+## Learned Plugin rule" in body["plugin_diffs"]["knowledge-init"]
        assert "knowledge-status" not in body["plugin_diffs"]

    def test_taskgen_job_end_to_end(self, studio_config, client, claude_skill):
        response = client.post(
            "/api/jobs",
            json={"type": "taskgen",
                  "params": {"skill_id": "claude--local-skill", "count": 3,
                             "guidance": "覆盖边界场景"}},
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        assert job_id.startswith("taskgen-")
        self._wait_status(client, job_id, "succeeded")

        body = client.get(f"/api/jobs/{job_id}/results").json()
        assert body["type"] == "taskgen"
        assert [t["id"] for t in body["tasks"]] == ["gen_000", "gen_001", "gen_002"]
        assert body["tasks"][0]["rubric"] == "Must satisfy criterion 0."
        assert body["summary"]["requested_count"] == 3
        assert body["summary"]["backend"] == "claude_code_exec"

    def test_multi_skill_taskgen_job_end_to_end(
        self, studio_config, client, plugin_skills
    ):
        response = client.post(
            "/api/jobs",
            json={
                "type": "taskgen",
                "params": {
                    "skill_ids": [
                        "claude-plugins--knowledge-init",
                        "claude-plugins--knowledge-status",
                    ],
                    "plugin": "knowledge",
                    "count": 2,
                },
            },
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        self._wait_status(client, job_id, "succeeded")
        body = client.get(f"/api/jobs/{job_id}/results").json()
        assert body["summary"]["skill_count"] == 2
        assert len(body["summary"]["skills"]) == 2

    def test_unsupported_type_message_lists_taskgen(self, client):
        response = client.post("/api/jobs", json={"type": "bogus", "params": {}})
        assert response.status_code == 400
        assert "taskgen" in response.json()["detail"]

    def test_post_eval_validation_400(self, client, claude_skill, single_taskset):
        response = client.post(
            "/api/jobs",
            json={"type": "eval", "params": {"skill_id": "claude--ghost", "taskset_id": "single-set"}},
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

        response = client.post(
            "/api/jobs",
            json={"type": "train", "params": {"skill_id": "claude--local-skill", "taskset_id": "ghost"}},
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

    def test_results_not_ready_404(self, client, studio_config):
        manager = JobManager(studio_config)
        job = manager.create_job("eval", {}, [runners.PYTHON, "-c", "import time; time.sleep(0)"])
        assert wait_until(lambda: manager.get_job(job.id).status == "succeeded")
        response = client.get(f"/api/jobs/{job.id}/results")
        assert response.status_code == 404
        assert "not available" in response.json()["detail"]

    def test_artifacts_list_read_and_traversal_400(self, studio_config, client, claude_skill, single_taskset):
        response = client.post(
            "/api/jobs",
            json={"type": "eval",
                  "params": {"skill_id": "claude--local-skill", "taskset_id": "single-set"}},
        )
        job_id = response.json()["id"]
        self._wait_status(client, job_id, "succeeded")

        listing = client.get(f"/api/jobs/{job_id}/artifacts").json()
        assert listing["kind"] == "dir"
        assert {f["name"] for f in listing["files"]} >= {"results.json", "report.md"}

        report = client.get(f"/api/jobs/{job_id}/artifacts", params={"path": "report.md"}).json()
        assert report["kind"] == "text"
        assert "stub" in report["content"]

        for bad in ("../job.json", "../../secrets", "/etc/passwd"):
            response = client.get(f"/api/jobs/{job_id}/artifacts", params={"path": bad})
            assert response.status_code == 400, bad
            assert "escapes" in response.json()["detail"] or "relative" in response.json()["detail"]

    def test_artifact_raw_download(self, studio_config, client, claude_skill, single_taskset):
        response = client.post(
            "/api/jobs",
            json={"type": "eval",
                  "params": {"skill_id": "claude--local-skill", "taskset_id": "single-set"}},
        )
        job_id = response.json()["id"]
        self._wait_status(client, job_id, "succeeded")

        raw = client.get(f"/api/jobs/{job_id}/artifacts/raw", params={"path": "report.md"})
        assert raw.status_code == 200
        assert "stub" in raw.text
        assert 'filename="report.md"' in raw.headers["content-disposition"]

        assert client.get(f"/api/jobs/{job_id}/artifacts/raw", params={"path": "nope.md"}).status_code == 404
        assert client.get(f"/api/jobs/{job_id}/artifacts/raw", params={"path": ""}).status_code == 404  # dir, not file
        assert client.get("/api/jobs/ghost/artifacts/raw", params={"path": "report.md"}).status_code == 404
        for bad in ("../job.json", "/etc/passwd"):
            response = client.get(f"/api/jobs/{job_id}/artifacts/raw", params={"path": bad})
            assert response.status_code == 400, bad

    def test_dashboard_aggregation(self, studio_config, client, claude_skill, single_taskset):
        done = client.post(
            "/api/jobs",
            json={"type": "eval",
                  "params": {"skill_id": "claude--local-skill", "taskset_id": "single-set"}},
        ).json()
        self._wait_status(client, done["id"], "succeeded")
        blocker = client.post(
            "/api/jobs", json={"type": "echo", "params": {"message": "will be cancelled"}}
        ).json()
        self._wait_status(client, blocker["id"], "succeeded")
        cancelled = client.post("/api/jobs", json={"type": "echo", "params": {}}).json()
        # cancel may race the fast echo; accept either terminal state and adapt expectations
        cancel_response = client.post(f"/api/jobs/{cancelled['id']}/cancel")
        final_states = {}
        for job_id in (done["id"], blocker["id"], cancelled["id"]):
            final_states[job_id] = client.get(f"/api/jobs/{job_id}").json()["status"]

        body = client.get("/api/dashboard").json()
        by_status = body["totals"]["by_status"]
        expected: dict[str, int] = {}
        for status in final_states.values():
            expected[status] = expected.get(status, 0) + 1
        assert by_status == expected
        assert len(body["recent"]) == 3
        eval_row = next(r for r in body["recent"] if r["type"] == "eval")
        assert eval_row["pass_rate"] == 0.5
        assert cancel_response.status_code in (200, 400)


class TestArtifactsFunctions:
    def _fake_job(self, studio_config, job_type="train", name="job-a") -> JobInfo:
        job_dir = studio_config.jobs_dir / name
        (job_dir / "out").mkdir(parents=True, exist_ok=True)
        return JobInfo(
            id=name, type=job_type, status="running",
            created_at="2026-07-06T00:00:00+00:00", params={},
            out_root=str(job_dir / "out"),
        )

    def test_skill_diff_nonempty(self, studio_config):
        job = self._fake_job(studio_config)
        out = Path(job.out_root)
        (out / "skills").mkdir(parents=True)
        (out / "skills" / "skill_v0000.md").write_text("# Skill\nOld line.\n", encoding="utf-8")
        (out / "best_skill.md").write_text("# Skill\nNew line.\n", encoding="utf-8")
        diff = artifacts.skill_diff(studio_config, job)
        assert "-Old line." in diff and "+New line." in diff
        assert diff.startswith("--- skills/skill_v0000.md")

    def test_skill_diff_missing_files_empty(self, studio_config):
        job = self._fake_job(studio_config, name="job-empty")
        assert artifacts.skill_diff(studio_config, job) == ""

    def test_job_progress_phrases(self, studio_config):
        job = self._fake_job(studio_config, name="job-log")
        log_path = studio_config.jobs_dir / job.id / "log.txt"

        log_path.write_text("[skilleval] tasks: 3 from /x/tasks.json\n", encoding="utf-8")
        assert artifacts.job_progress(studio_config, job) == "rollout 3 tasks"

        log_path.write_text("...\n  [STEP 2 done] epoch=1 action=accept\n", encoding="utf-8")
        assert artifacts.job_progress(studio_config, job) == "step 2 done"

        finished = job.model_copy(update={"status": "succeeded"})
        assert artifacts.job_progress(studio_config, finished) == "succeeded"
        queued = job.model_copy(update={"status": "queued"})
        assert artifacts.job_progress(studio_config, queued) == "queued"

    def test_train_summary_mid_run_without_summary_json(self, studio_config):
        job = self._fake_job(studio_config, name="job-mid")
        out = Path(job.out_root)
        history = [{"step": 1, "epoch": 1, "action": "accept", "selection_hard": 0.3,
                    "selection_soft": 0.4, "current_score": 0.3, "best_score": 0.3,
                    "best_step": 1, "skill_len": 100, "wall_time_s": 5.0}]
        (out / "history.json").write_text(json.dumps(history), encoding="utf-8")
        summary = artifacts.train_summary(studio_config, job)
        assert summary["finished"] is False
        assert summary["best_step"] == 1
        assert len(summary["steps"]) == 1

    def test_plugin_train_summary_mid_run_uses_job_mode(self, studio_config):
        job = self._fake_job(studio_config, name="job-plugin-mid").model_copy(
            update={
                "params": {
                    "target_mode": "plugin",
                    "skill_ids": ["skill-a", "skill-b"],
                }
            }
        )
        out = Path(job.out_root)
        history = [
            {
                "step": 1,
                "epoch": 1,
                "action": "reject",
                "candidate_aggregates": {
                    "overall": {"count": 2, "hard": 0.5, "soft": 0.6}
                },
                "current_score": 0.4,
                "best_score": 0.4,
                "best_step": 0,
            }
        ]
        (out / "history.json").write_text(json.dumps(history), encoding="utf-8")

        summary = artifacts.train_summary(studio_config, job)

        assert summary["mode"] == "plugin"
        assert summary["finished"] is False
        assert summary["steps"][0]["selection_hard"] == 0.5
        assert summary["steps"][0]["selection_soft"] == 0.6

    def test_read_artifact_binary_returns_meta_only(self, studio_config):
        job = self._fake_job(studio_config, name="job-bin")
        out = Path(job.out_root)
        (out / "blob.bin").write_bytes(b"\x00\x01\x02data")
        result = artifacts.read_artifact(studio_config, job, "blob.bin")
        assert result["kind"] == "binary"
        assert "content" not in result


class TestSecurityHardening:
    """Named security regression tests: traversal, ranges, bind address."""

    def test_artifacts_traversal_dotdot_rejected(self, studio_config):
        job_dir = studio_config.jobs_dir / "sec-job"
        (job_dir / "out").mkdir(parents=True)
        (job_dir / "secret.txt").write_text("outside out/", encoding="utf-8")
        job = JobInfo(id="sec-job", type="eval", status="succeeded",
                      created_at="2026-07-06T00:00:00+00:00", out_root=str(job_dir / "out"))
        with pytest.raises(ValueError, match="escapes"):
            artifacts.read_artifact(studio_config, job, "../secret.txt")

    def test_artifacts_traversal_absolute_rejected(self, studio_config):
        job_dir = studio_config.jobs_dir / "sec-job2"
        (job_dir / "out").mkdir(parents=True)
        job = JobInfo(id="sec-job2", type="eval", status="succeeded",
                      created_at="2026-07-06T00:00:00+00:00", out_root=str(job_dir / "out"))
        with pytest.raises(ValueError, match="relative"):
            artifacts.list_artifacts(studio_config, job, "/etc")

    def test_artifacts_traversal_urlencoded_rejected_via_api(self, studio_config, stub_scripts):
        """%2e%2e-encoded traversal decodes to ../ at the ASGI layer and must 400."""
        app = create_app(studio_config)
        with TestClient(app) as client:
            manager = JobManager(studio_config)
            job = manager.create_job("eval", {}, [runners.PYTHON, "-c", "print('x')"])
            assert wait_until(lambda: manager.get_job(job.id).status == "succeeded")
            response = client.get(
                f"/api/jobs/{job.id}/artifacts?path=%2e%2e%2fjob.json"
            )
            assert response.status_code == 400
            assert "escapes" in response.json()["detail"]

    def test_eval_params_out_of_range_rejected(self, studio_config, stub_scripts, claude_skill, single_taskset):
        for params, field in (
            ({"workers": 99}, "workers"),
            ({"workers": 0}, "workers"),
            ({"timeout": 10}, "timeout"),
            ({"timeout": 999999}, "timeout"),
            ({"limit": -1}, "limit"),
        ):
            full = {"skill_id": "claude--local-skill", "taskset_id": "single-set", **params}
            with pytest.raises(ValueError, match=field):
                build_eval_command(studio_config, full, job_dir_of(studio_config))

    def test_train_params_out_of_range_rejected(self, studio_config, stub_scripts, claude_skill, single_taskset):
        base = {"skill_id": "claude--local-skill", "taskset_id": "single-set"}
        for params, field in (
            ({"num_epochs": 0}, "num_epochs"),
            ({"num_epochs": 11}, "num_epochs"),
            ({"learning_rate": 17}, "learning_rate"),
            ({"gate_metric": "bogus"}, "gate_metric"),
            ({"split_ratio": "4:3"}, "split_ratio"),
            ({"split_ratio": "a:b:c"}, "split_ratio"),
        ):
            with pytest.raises(ValueError, match=field):
                build_train_command(studio_config, {**base, **params}, job_dir_of(studio_config, "rng"))

    def test_main_defaults_bind_localhost(self, monkeypatch):
        import skillopt_studio.__main__ as studio_main

        captured = {}
        monkeypatch.setattr(
            studio_main.uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs)
        )
        studio_main.main([])
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 8321
        assert captured["reload"] is False


class TestBackendSelection:
    """Exec-backend dropdown support: validation, CLI detection, train override."""

    def _eval_params(self, **extra) -> dict:
        return {"skill_id": "claude--local-skill", "taskset_id": "single-set", **extra}

    def test_eval_default_backend_is_claude(self, studio_config, stub_scripts, claude_skill, single_taskset):
        argv = build_eval_command(studio_config, self._eval_params(), job_dir_of(studio_config))
        assert argv[argv.index("--target_backend") + 1] == "claude_code_exec"

    def test_eval_codex_backend_argv(self, studio_config, stub_scripts, claude_skill, single_taskset):
        argv = build_eval_command(
            studio_config, self._eval_params(target_backend="codex_exec"), job_dir_of(studio_config)
        )
        assert argv[argv.index("--target_backend") + 1] == "codex_exec"
        assert "--model" not in argv  # empty model → evaluate_skill uses the backend default

    def test_eval_unknown_backend_rejected(self, studio_config, stub_scripts, claude_skill, single_taskset):
        with pytest.raises(ValueError, match="target_backend"):
            build_eval_command(
                studio_config, self._eval_params(target_backend="qwen_chat"), job_dir_of(studio_config)
            )

    def test_eval_backend_cli_missing_rejected(self, studio_config, stub_scripts, monkeypatch,
                                               claude_skill, single_taskset):
        monkeypatch.setattr(runners, "cli_path", lambda backend: None)
        with pytest.raises(ValueError, match="'codex' CLI"):
            build_eval_command(
                studio_config, self._eval_params(target_backend="codex_exec"), job_dir_of(studio_config)
            )

    def test_train_backend_override_codex_default_model(self, studio_config, stub_scripts,
                                                        claude_skill, single_taskset):
        from skillopt.model.common import default_model_for_backend

        job_dir = job_dir_of(studio_config, "train-codex")
        build_train_command(
            studio_config, self._eval_params(target_backend="codex_exec"), job_dir
        )
        cfg = yaml.safe_load((job_dir / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["model"]["target_backend"] == "codex_exec"
        assert cfg["model"]["target"] == default_model_for_backend("codex_exec")

    def test_train_backend_codex_explicit_model_kept(self, studio_config, stub_scripts,
                                                     claude_skill, single_taskset):
        job_dir = job_dir_of(studio_config, "train-codex-explicit")
        build_train_command(
            studio_config,
            self._eval_params(target_backend="codex_exec", target_model="gpt-5.5-codex"),
            job_dir,
        )
        cfg = yaml.safe_load((job_dir / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["model"]["target_backend"] == "codex_exec"
        assert cfg["model"]["target"] == "gpt-5.5-codex"

    def test_train_default_backend_unchanged(self, studio_config, stub_scripts, claude_skill, single_taskset):
        job_dir = job_dir_of(studio_config, "train-default-backend")
        build_train_command(studio_config, self._eval_params(), job_dir)
        cfg = yaml.safe_load((job_dir / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["model"]["target_backend"] == "claude_code_exec"
        assert cfg["model"]["target"] == "claude-opus-4-8"  # YAML default untouched

    def test_train_backend_cli_missing_rejected(self, studio_config, stub_scripts, monkeypatch,
                                                claude_skill, single_taskset):
        monkeypatch.setattr(runners, "cli_path", lambda backend: None)
        with pytest.raises(ValueError, match="CLI"):
            build_train_command(
                studio_config, self._eval_params(target_backend="codex_exec"),
                job_dir_of(studio_config, "train-no-cli"),
            )

    def test_environment_endpoint_reports_cli_status(self, studio_config, monkeypatch):
        monkeypatch.setattr(
            runners,
            "cli_path",
            lambda backend: "/stub/bin/claude" if backend == "claude_code_exec" else None,
        )
        app = create_app(studio_config)
        with TestClient(app) as client:
            body = client.get("/api/environment").json()
        by_backend = {entry["backend"]: entry for entry in body["backends"]}
        assert by_backend["claude_code_exec"]["available"] is True
        assert by_backend["claude_code_exec"]["path"] == "/stub/bin/claude"
        assert by_backend["claude_code_exec"]["cli"] == "claude"
        assert by_backend["codex_exec"]["available"] is False
        assert by_backend["codex_exec"]["path"] is None
