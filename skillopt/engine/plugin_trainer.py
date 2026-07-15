"""Directed multi-Skill optimization with complete-Plugin validation."""
from __future__ import annotations

import json
import math
import os
import shutil
import time
from collections import Counter
from typing import Any

from skillopt.engine.trainer import _normalise_patches
from skillopt.envs.skilleval.adapter import SkillEvalAdapter
from skillopt.envs.skilleval.plugin import (
    PluginState,
    aggregate_results,
    load_plugin_snapshot,
    plugin_hash,
    write_plugin_snapshot,
)
from skillopt.evaluation.plugin_gate import (
    FailureAttribution,
    attribute_failures,
    evaluate_plugin_gate,
    metric_scores,
    select_responsible_skills,
    validate_plugin_coverage,
)
from skillopt.gradient.aggregate import merge_patches
from skillopt.model import get_token_summary
from skillopt.optimizer.clip import rank_and_select
from skillopt.optimizer.skill import apply_patch_with_report


def _read_json(path: str, default: Any) -> Any:
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


class PluginTrainer:
    """A focused Plugin-state training loop for SkillEval tasks."""

    def __init__(
        self,
        cfg: dict,
        adapter: SkillEvalAdapter,
        initial_state: PluginState,
    ) -> None:
        self.cfg = dict(cfg)
        self.adapter = adapter
        self.initial_state = initial_state
        self.dataloader = None
        self._selection_items: list[dict] = []
        self._prepared = False

    def preflight(self) -> None:
        """Load and validate all local inputs before any model configuration."""
        cfg = self.cfg
        if cfg.get("skill_update_mode", "patch") != "patch":
            raise ValueError("Plugin training currently supports skill_update_mode=patch only")
        if cfg.get("use_slow_update", False):
            raise ValueError("Plugin training does not support use_slow_update")
        if cfg.get("use_meta_skill", False):
            raise ValueError("Plugin training does not support use_meta_skill")
        if int(cfg.get("accumulation", 1) or 1) != 1:
            raise ValueError("Plugin training currently requires accumulation=1")
        if not self.initial_state.trainable_names:
            raise ValueError("Plugin training requires at least one trainable Skill")

        raw_max_skills = cfg.get("max_skills_per_candidate", 2)
        max_skills = int(2 if raw_max_skills is None else raw_max_skills)
        if max_skills <= 0:
            raise ValueError("max_skills_per_candidate must be positive")
        if max_skills > len(self.initial_state.trainable_names):
            cfg["max_skills_per_candidate"] = len(self.initial_state.trainable_names)

        max_regression = float(cfg.get("max_skill_regression", 0.0) or 0.0)
        if not 0.0 <= max_regression <= 1.0:
            raise ValueError("max_skill_regression must be in [0, 1]")

        metric = str(cfg.get("gate_metric", "hard")).strip().lower()
        if metric not in {"hard", "soft", "mixed"}:
            raise ValueError("gate_metric must be hard, soft, or mixed")
        cfg["gate_metric"] = metric
        mixed_weight = float(cfg.get("gate_mixed_weight", 0.5))
        if not 0.0 <= mixed_weight <= 1.0:
            raise ValueError("gate_mixed_weight must be in [0, 1]")

        cfg["plugin_skill_names"] = list(self.initial_state.names)
        cfg["required_validation_skills"] = list(
            self.initial_state.trainable_names
        )
        self.adapter.setup(cfg)
        self.dataloader = self.adapter.get_dataloader()

        selection_limit = int(cfg.get("sel_env_num", 0) or 0)
        self._selection_items = list(self.dataloader.val_items)
        if selection_limit > 0:
            self._selection_items = self._selection_items[:selection_limit]
        validate_plugin_coverage(
            self._selection_items,
            list(self.initial_state.trainable_names),
        )

        batch_size = int(cfg.get("batch_size", 0) or 0)
        num_epochs = int(cfg.get("num_epochs", 0) or 0)
        edit_budget = int(cfg.get("edit_budget", 0) or 0)
        if min(batch_size, num_epochs, edit_budget) <= 0:
            raise ValueError("batch_size, num_epochs, and edit_budget must be positive")

        train_size = len(self.dataloader.train_items)
        configured_train_size = int(cfg.get("train_size", 0) or 0)
        if configured_train_size and configured_train_size != train_size:
            raise ValueError(
                f"Configured train_size={configured_train_size} does not match "
                f"loaded train split size={train_size}"
            )
        cfg["train_size"] = train_size
        cfg["steps_per_epoch"] = math.ceil(train_size / batch_size)
        cfg["total_steps"] = num_epochs * cfg["steps_per_epoch"]
        self._prepared = True

    def _aggregate(self, results: list[dict]) -> dict:
        return aggregate_results(results, list(self.initial_state.names), require_valid=True)

    def _rollout(
        self,
        items: list[dict],
        state: PluginState,
        out_dir: str,
    ) -> tuple[list[dict], dict]:
        results = self.adapter.rollout_plugin(items, state, out_dir)
        return results, self._aggregate(results)

    def _snapshot_path(self, step: int) -> str:
        return os.path.join(
            self.cfg["out_root"],
            "plugin_versions",
            f"plugin_v{step:04d}",
        )

    def _save_best(self, state: PluginState) -> None:
        write_plugin_snapshot(
            state,
            os.path.join(self.cfg["out_root"], "best_plugin"),
            replace_existing=True,
        )

    def _resume(self) -> tuple[PluginState, dict | None, int]:
        out_root = self.cfg["out_root"]
        runtime = _read_json(os.path.join(out_root, "runtime_state.json"), None)
        if not isinstance(runtime, dict):
            return self.initial_state, None, 1
        snapshot = runtime.get("current_snapshot")
        if not isinstance(snapshot, str):
            raise ValueError("runtime_state.json has no current_snapshot")
        state = load_plugin_snapshot(snapshot)
        if state.names != self.initial_state.names:
            raise ValueError(
                "resume Plugin Skill order differs from current inputs: "
                f"{state.names!r} != {self.initial_state.names!r}"
            )
        if state.trainable_names != self.initial_state.trainable_names:
            raise ValueError(
                "resume trainable Skill set differs from current inputs: "
                f"{state.trainable_names!r} != {self.initial_state.trainable_names!r}"
            )
        aggregates = runtime.get("current_aggregates")
        if not isinstance(aggregates, dict):
            raise ValueError("runtime_state.json has no current_aggregates")
        return state, aggregates, int(runtime.get("last_completed_step", 0)) + 1

    def _persist_runtime(
        self,
        step: int,
        state: PluginState,
        aggregates: dict,
        snapshot_path: str,
    ) -> None:
        overall_score, per_skill = metric_scores(
            aggregates,
            list(state.trainable_names),
            self.cfg["gate_metric"],
            float(self.cfg.get("gate_mixed_weight", 0.5)),
        )
        _write_json_atomic(
            os.path.join(self.cfg["out_root"], "runtime_state.json"),
            {
                "last_completed_step": step,
                "current_snapshot": snapshot_path,
                "best_snapshot": os.path.join(self.cfg["out_root"], "best_plugin"),
                "plugin_hash": plugin_hash(state),
                "skill_names": list(state.names),
                "trainable_skill_names": list(state.trainable_names),
                "current_score": overall_score,
                "current_skill_scores": per_skill,
                "current_aggregates": aggregates,
            },
        )

    def _step_candidate(
        self,
        state: PluginState,
        results: list[dict],
        attributions: list[FailureAttribution],
        selected_names: list[str],
        step_dir: str,
        seed: int,
    ) -> tuple[PluginState, dict[str, dict]]:
        updates: dict[str, str] = {}
        reports: dict[str, dict] = {}
        by_task = {attribution.task_id: attribution for attribution in attributions}

        for offset, name in enumerate(selected_names):
            skill = state.skill(name)
            skill_results = [
                result
                for result in results
                if name
                in by_task.get(
                    str(result.get("id")),
                    FailureAttribution("", "execution", (), False),
                ).responsible_skills
            ]
            skill_dir = os.path.join(step_dir, "skills", name)
            patches_dir = os.path.join(skill_dir, "patches")
            raw = self.adapter.reflect(
                skill_results,
                skill.content,
                skill_dir,
                prediction_dir=os.path.join(step_dir, "rollout", "predictions"),
                patches_dir=patches_dir,
                random_seed=seed + offset,
            )
            failure, success = _normalise_patches(raw, update_mode="patch")
            report: dict[str, Any] = {
                "attributed_task_ids": [str(result.get("id")) for result in skill_results],
                "failure_patches": len(failure),
                "success_patches": len(success),
            }
            if not failure and not success:
                report["action"] = "skip_no_patches"
                reports[name] = report
                continue

            merged = merge_patches(
                skill.content,
                failure,
                success,
                batch_size=int(self.cfg.get("merge_batch_size", 4) or 4),
                workers=int(self.cfg.get("analyst_workers", 4) or 4),
                update_mode="patch",
            )
            ranked = rank_and_select(
                skill.content,
                merged,
                max_edits=int(self.cfg["edit_budget"]),
                update_mode="patch",
            )
            candidate_content, apply_report = apply_patch_with_report(skill.content, ranked)
            os.makedirs(skill_dir, exist_ok=True)
            _write_json_atomic(os.path.join(skill_dir, "merged_patch.json"), merged)
            _write_json_atomic(os.path.join(skill_dir, "ranked_edits.json"), ranked)
            _write_json_atomic(
                os.path.join(skill_dir, "edit_apply_report.json"),
                apply_report,
            )
            with open(
                os.path.join(skill_dir, "candidate_skill.md"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(candidate_content)
            report["apply_report"] = apply_report
            if candidate_content == skill.content:
                report["action"] = "skip_no_applied_edits"
            else:
                report["action"] = "updated"
                updates[name] = candidate_content
            reports[name] = report

        return state.replace_content(updates) if updates else state, reports

    def train(self) -> dict:
        if not self._prepared:
            raise RuntimeError("PluginTrainer.preflight() must run before train()")
        cfg = self.cfg
        out_root = os.path.abspath(cfg["out_root"])
        cfg["out_root"] = out_root
        os.makedirs(out_root, exist_ok=True)
        _write_json_atomic(os.path.join(out_root, "config.json"), cfg)

        history = _read_json(os.path.join(out_root, "history.json"), [])
        if not isinstance(history, list):
            raise ValueError("history.json must contain a list")
        current_state, current_aggregates, resume_from = self._resume()

        if current_aggregates is None:
            initial_snapshot = self._snapshot_path(0)
            write_plugin_snapshot(current_state, initial_snapshot)
            _, current_aggregates = self._rollout(
                self._selection_items,
                current_state,
                os.path.join(out_root, "selection_eval_baseline"),
            )
            self._save_best(current_state)
            self._persist_runtime(0, current_state, current_aggregates, initial_snapshot)

        baseline_aggregates = current_aggregates
        runtime = _read_json(os.path.join(out_root, "runtime_state.json"), {})
        baseline_path = os.path.join(out_root, "selection_eval_baseline", "summary.json")
        if os.path.isfile(baseline_path):
            baseline_aggregates = _read_json(baseline_path, current_aggregates)
        else:
            _write_json_atomic(baseline_path, current_aggregates)

        global_step = 0
        total_steps = int(cfg["total_steps"])
        steps_per_epoch = int(cfg["steps_per_epoch"])
        for epoch in range(1, int(cfg["num_epochs"]) + 1):
            batches = self.dataloader.plan_train_epoch(
                epoch=epoch,
                steps_per_epoch=steps_per_epoch,
                accumulation=1,
                batch_size=int(cfg["batch_size"]),
                seed=int(cfg.get("seed", 42)),
                out_root=out_root,
            )
            for step_in_epoch, batch in enumerate(batches):
                global_step += 1
                if global_step < resume_from:
                    continue
                started = time.time()
                step_dir = os.path.join(out_root, "steps", f"step_{global_step:04d}")
                os.makedirs(step_dir, exist_ok=True)
                items = list(batch.payload or [])
                train_results, train_aggregates = self._rollout(
                    items,
                    current_state,
                    os.path.join(step_dir, "rollout"),
                )
                attributions = attribute_failures(
                    train_results,
                    current_state.trainable_names,
                )
                selected = select_responsible_skills(
                    attributions,
                    current_state.trainable_names,
                    int(cfg.get("max_skills_per_candidate", 2)),
                )
                attribution_rows = [entry.to_dict() for entry in attributions]
                _write_json_atomic(
                    os.path.join(step_dir, "attribution.json"),
                    attribution_rows,
                )
                with open(
                    os.path.join(out_root, "attribution.jsonl"),
                    "a",
                    encoding="utf-8",
                ) as f:
                    f.write(
                        json.dumps(
                            {"step": global_step, "attributions": attribution_rows},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

                step_record: dict[str, Any] = {
                    "step": global_step,
                    "epoch": epoch,
                    "step_in_epoch": step_in_epoch,
                    "train_aggregates": train_aggregates,
                    "selected_skills": selected,
                    "attribution_counts": dict(
                        Counter(entry.category for entry in attributions)
                    ),
                }
                if not selected:
                    step_record["action"] = "skip_no_attribution"
                    candidate_state = current_state
                    skill_reports: dict[str, dict] = {}
                else:
                    candidate_state, skill_reports = self._step_candidate(
                        current_state,
                        train_results,
                        attributions,
                        selected,
                        step_dir,
                        batch.seed,
                    )
                    step_record["skill_reports"] = skill_reports
                    if plugin_hash(candidate_state) == plugin_hash(current_state):
                        step_record["action"] = "skip_no_applied_edits"
                    else:
                        candidate_dir = os.path.join(step_dir, "candidate_plugin")
                        write_plugin_snapshot(candidate_state, candidate_dir)
                        _, candidate_aggregates = self._rollout(
                            self._selection_items,
                            candidate_state,
                            os.path.join(step_dir, "selection_eval"),
                        )
                        gate = evaluate_plugin_gate(
                            current_aggregates,
                            candidate_aggregates,
                            list(current_state.trainable_names),
                            metric=cfg["gate_metric"],
                            mixed_weight=float(cfg.get("gate_mixed_weight", 0.5)),
                            max_skill_regression=float(
                                cfg.get("max_skill_regression", 0.0)
                            ),
                        )
                        step_record.update(
                            {
                                "action": gate.action,
                                "candidate_aggregates": candidate_aggregates,
                                "candidate_score": gate.overall_score,
                                "regressions": gate.regressions,
                                "gate_reasons": list(gate.reasons),
                            }
                        )
                        if gate.action == "accept_new_best":
                            current_state = candidate_state
                            current_aggregates = candidate_aggregates
                            accepted_snapshot = self._snapshot_path(global_step)
                            write_plugin_snapshot(current_state, accepted_snapshot)
                            self._save_best(current_state)
                            runtime["current_snapshot"] = accepted_snapshot

                current_score, current_skill_scores = metric_scores(
                    current_aggregates,
                    list(current_state.trainable_names),
                    cfg["gate_metric"],
                    float(cfg.get("gate_mixed_weight", 0.5)),
                )
                current_snapshot = runtime.get("current_snapshot")
                if not isinstance(current_snapshot, str):
                    current_snapshot = self._snapshot_path(0)
                step_record.update(
                    {
                        "current_score": current_score,
                        "best_score": current_score,
                        "best_step": max(
                            (
                                int(row["step"])
                                for row in history + [step_record]
                                if row.get("action") == "accept_new_best"
                            ),
                            default=0,
                        ),
                        "current_skill_scores": current_skill_scores,
                        "plugin_hash": plugin_hash(current_state),
                        "wall_time_s": round(time.time() - started, 2),
                    }
                )
                history.append(step_record)
                _write_json_atomic(os.path.join(out_root, "history.json"), history)
                _write_json_atomic(
                    os.path.join(step_dir, "step_record.json"),
                    step_record,
                )
                self._persist_runtime(
                    global_step,
                    current_state,
                    current_aggregates,
                    current_snapshot,
                )
                runtime = _read_json(os.path.join(out_root, "runtime_state.json"), {})
                print(
                    f"  [PLUGIN STEP {global_step}/{total_steps}] "
                    f"action={step_record['action']} score={current_score:.4f}"
                )

        test_aggregates = None
        if cfg.get("eval_test", True):
            test_items = list(self.dataloader.test_items)
            test_limit = int(cfg.get("test_env_num", 0) or 0)
            if test_limit > 0:
                test_items = test_items[:test_limit]
            _, test_aggregates = self._rollout(
                test_items,
                current_state,
                os.path.join(out_root, "test_eval"),
            )

        actions = Counter(str(row.get("action")) for row in history)
        current_score, current_skill_scores = metric_scores(
            current_aggregates,
            list(current_state.trainable_names),
            cfg["gate_metric"],
            float(cfg.get("gate_mixed_weight", 0.5)),
        )
        summary = {
            "mode": "plugin",
            "skill_names": list(current_state.names),
            "trainable_skill_names": list(current_state.trainable_names),
            "baseline_aggregates": baseline_aggregates,
            "best_aggregates": current_aggregates,
            "test_aggregates": test_aggregates,
            "best_selection_score": current_score,
            "best_skill_scores": current_skill_scores,
            "best_step": max(
                (
                    int(row["step"])
                    for row in history
                    if row.get("action") == "accept_new_best"
                ),
                default=0,
            ),
            "total_steps": len(history),
            "total_accepts": actions["accept_new_best"],
            "total_rejects": actions["reject"],
            "total_skips": sum(
                count for action, count in actions.items() if action.startswith("skip_")
            ),
            "token_summary": get_token_summary(),
        }
        _write_json_atomic(os.path.join(out_root, "summary.json"), summary)
        self._save_best(current_state)
        return summary
