"""Pure Plugin training contracts: state, attribution, gate, and snapshots."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

from skillopt.engine.plugin_trainer import PluginTrainer
from skillopt.envs.skilleval.adapter import SkillEvalAdapter
from skillopt.envs.skilleval.plugin import (
    PluginState,
    collect_plugin_state,
    load_plugin_snapshot,
    plugin_hash,
    write_plugin_snapshot,
)
from skillopt.evaluation.plugin_gate import (
    attribute_failures,
    evaluate_plugin_gate,
    select_responsible_skills,
    validate_plugin_coverage,
)

_TRAIN_PLUGIN_SPEC = importlib.util.spec_from_file_location(
    "train_plugin",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "train_plugin.py"),
)
train_plugin = importlib.util.module_from_spec(_TRAIN_PLUGIN_SPEC)
sys.modules.setdefault("train_plugin", train_plugin)
_TRAIN_PLUGIN_SPEC.loader.exec_module(train_plugin)


def _make_skill(tmp_path, name: str, *, support: bool = False):
    root = tmp_path / name
    root.mkdir()
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8"
    )
    if support:
        (root / "references").mkdir()
        (root / "references" / "rules.md").write_text("rules", encoding="utf-8")
    return root


def _aggregate(
    overall: tuple[float, float],
    **by_skill: tuple[float, float],
) -> dict:
    return {
        "overall": {"count": 4, "hard": overall[0], "soft": overall[1]},
        "by_skill": {
            name: {"count": 2, "hard": values[0], "soft": values[1]}
            for name, values in by_skill.items()
        },
    }


class TestPluginState:
    def test_collect_replace_and_runtime_keep_named_documents(self, tmp_path) -> None:
        alpha = _make_skill(tmp_path, "alpha", support=True)
        beta = _make_skill(tmp_path, "beta")
        state = collect_plugin_state([str(alpha), str(beta)], ["alpha"])

        assert state.names == ("alpha", "beta")
        assert state.trainable_names == ("alpha",)
        candidate = state.replace_content({"alpha": "# changed"})
        assert candidate.skill("alpha").content == "# changed"
        assert candidate.skill("beta").content == state.skill("beta").content
        runtime = candidate.runtime_skills()
        assert [skill["name"] for skill in runtime] == ["alpha", "beta"]
        assert runtime[0]["files"][0][1] == "references/rules.md"

    def test_rejects_unknown_or_frozen_updates(self, tmp_path) -> None:
        state = collect_plugin_state(
            [str(_make_skill(tmp_path, "alpha")), str(_make_skill(tmp_path, "beta"))],
            ["alpha"],
        )
        with pytest.raises(ValueError, match="unknown"):
            state.replace_content({"ghost": "x"})
        with pytest.raises(ValueError, match="non-trainable"):
            state.replace_content({"beta": "x"})

    def test_snapshot_roundtrip_is_complete_and_hashed(self, tmp_path) -> None:
        state = collect_plugin_state(
            [
                str(_make_skill(tmp_path, "alpha", support=True)),
                str(_make_skill(tmp_path, "beta")),
            ]
        )
        destination = tmp_path / "plugin_v0000"
        manifest = write_plugin_snapshot(state, str(destination))
        loaded = load_plugin_snapshot(str(destination))

        assert manifest["skill_names"] == ["alpha", "beta"]
        assert loaded.names == state.names
        assert plugin_hash(loaded) == plugin_hash(state)
        assert (
            destination / "alpha" / "references" / "rules.md"
        ).read_text(encoding="utf-8") == "rules"

        support_path = destination / "alpha" / "references" / "rules.md"
        support_path.write_text("tampered", encoding="utf-8")
        with pytest.raises(ValueError, match="hash mismatch"):
            load_plugin_snapshot(str(destination))
        support_path.write_text("rules", encoding="utf-8")

        manifest_path = destination / "manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["plugin_hash"] = "bad"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError, match="hash mismatch"):
            load_plugin_snapshot(str(destination))


class TestFailureAttribution:
    def test_categories_and_task_failures_do_not_create_gradients(self) -> None:
        results = [
            {"id": "route", "hard": 0, "task_type": "routing", "target_skills": ["alpha"]},
            {
                "id": "handoff",
                "hard": 0,
                "task_type": "integration",
                "target_skills": ["alpha", "beta"],
            },
            {
                "id": "shared",
                "hard": 0,
                "task_type": "shared_dependency",
                "target_skills": ["beta"],
            },
            {"id": "exec", "hard": 0, "task_type": "default", "target_skills": ["beta"]},
            {"id": "run-error", "hard": 0, "error": "boom", "target_skills": ["alpha"]},
            {"id": "judge-error", "hard": 0, "judge_error": "bad", "target_skills": ["beta"]},
            {"id": "pass", "hard": 1, "target_skills": ["alpha"]},
        ]
        attrs = attribute_failures(results, ["alpha", "beta"])
        assert [attr.category for attr in attrs] == [
            "routing",
            "handoff",
            "shared_dependency",
            "execution",
            "task_failure",
            "judge_failure",
        ]
        assert attrs[-2].responsible_skills == ()
        assert attrs[-1].gradient_eligible is False
        assert select_responsible_skills(attrs, ["alpha", "beta"], 1) == ["beta"]

    def test_selection_tie_uses_runtime_order(self) -> None:
        attrs = attribute_failures(
            [
                {"id": "a", "hard": 0, "target_skills": ["alpha"]},
                {"id": "b", "hard": 0, "target_skills": ["beta"]},
            ],
            ["alpha", "beta"],
        )
        assert select_responsible_skills(attrs, ["alpha", "beta"], 2) == [
            "alpha",
            "beta",
        ]


class TestPluginGate:
    def test_rejects_overall_improvement_with_skill_regression(self) -> None:
        current = _aggregate((0.50, 0.60), alpha=(0.50, 0.60), beta=(0.50, 0.60))
        candidate = _aggregate((0.60, 0.70), alpha=(0.80, 0.80), beta=(0.40, 0.50))
        gate = evaluate_plugin_gate(current, candidate, ["alpha", "beta"])
        assert gate.action == "reject"
        assert gate.overall_score == 0.60
        assert gate.regressions["beta"] == pytest.approx(0.10)
        assert "beta regressed" in gate.reasons[0]

    def test_accepts_strict_non_regressing_improvement(self) -> None:
        current = _aggregate((0.50, 0.60), alpha=(0.50, 0.60), beta=(0.50, 0.60))
        candidate = _aggregate((0.60, 0.70), alpha=(0.70, 0.80), beta=(0.50, 0.65))
        gate = evaluate_plugin_gate(
            current,
            candidate,
            ["alpha", "beta"],
            metric="mixed",
            mixed_weight=0.5,
        )
        assert gate.action == "accept_new_best"
        assert gate.reasons == ()

    def test_coverage_requires_every_skill(self) -> None:
        with pytest.raises(ValueError, match="beta"):
            validate_plugin_coverage(
                [{"target_skills": ["alpha"]}],
                ["alpha", "beta"],
            )


def _write_split(tmp_path, train: list[dict], val: list[dict], test: list[dict]):
    root = tmp_path / "split"
    for name, items in (("train", train), ("val", val), ("test", test)):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "items.json").write_text(
            json.dumps(items), encoding="utf-8"
        )
    return root


def _task(
    task_id: str,
    targets: list[str] | None,
    task_type: str = "default",
) -> dict:
    task = {
        "id": task_id,
        "question": f"Question {task_id}",
        "rubric": "Pass.",
        "task_type": task_type,
    }
    if targets is not None:
        task["target_skills"] = targets
    return task


def _trainer_cfg(tmp_path, split_root, **overrides) -> dict:
    cfg = {
        "out_root": str(tmp_path / "out"),
        "split_mode": "split_dir",
        "split_dir": str(split_root),
        "batch_size": 2,
        "num_epochs": 1,
        "accumulation": 1,
        "seed": 42,
        "edit_budget": 1,
        "merge_batch_size": 4,
        "analyst_workers": 1,
        "minibatch_size": 2,
        "failure_only": True,
        "skill_update_mode": "patch",
        "use_slow_update": False,
        "use_meta_skill": False,
        "max_skills_per_candidate": 2,
        "max_skill_regression": 0.0,
        "gate_metric": "hard",
        "gate_mixed_weight": 0.5,
        "sel_env_num": 0,
        "test_env_num": 0,
        "eval_test": True,
        "workers": 1,
        "timeout": 60,
    }
    cfg.update(overrides)
    return cfg


def _patch_append_reflection(monkeypatch, adapter, calls: list[str]) -> None:
    def fake_reflect(results, skill_content, out_dir, **kwargs):
        del results, kwargs
        calls.append(out_dir)
        return [
            {
                "source_type": "failure",
                "batch_size": 1,
                "patch": {
                    "reasoning": "stub",
                    "edits": [{"op": "append", "content": "## Improved"}],
                },
            }
        ]

    monkeypatch.setattr(adapter, "reflect", fake_reflect)


class TestPluginTrainingCli:
    def test_configure_models_applies_role_specific_settings(self, monkeypatch) -> None:
        calls: dict[str, dict] = {}
        for name in (
            "configure_azure_openai",
            "configure_qwen_chat",
            "configure_minimax_chat",
            "configure_codex_exec",
            "configure_claude_code_exec",
        ):
            monkeypatch.setattr(
                train_plugin,
                name,
                lambda _name=name, **kwargs: calls.__setitem__(_name, kwargs),
            )
        for name in (
            "set_optimizer_backend",
            "set_target_backend",
            "set_optimizer_deployment",
            "set_target_deployment",
            "set_reasoning_effort",
            "reset_token_tracker",
        ):
            monkeypatch.setattr(train_plugin, name, lambda *args, **kwargs: None)

        train_plugin._configure_models(
            {
                "optimizer_model": "optimizer",
                "target_model": "target",
                "optimizer_azure_openai_endpoint": "https://optimizer.openai",
                "target_azure_openai_endpoint": "https://target.openai",
                "optimizer_qwen_chat_base_url": "https://optimizer.qwen",
                "target_qwen_chat_base_url": "https://target.qwen",
                "minimax_base_url": "https://minimax",
            }
        )

        assert calls["configure_azure_openai"]["optimizer_endpoint"] == (
            "https://optimizer.openai"
        )
        assert calls["configure_azure_openai"]["target_endpoint"] == (
            "https://target.openai"
        )
        assert calls["configure_qwen_chat"]["optimizer_base_url"] == (
            "https://optimizer.qwen"
        )
        assert calls["configure_qwen_chat"]["target_base_url"] == "https://target.qwen"
        assert calls["configure_minimax_chat"]["base_url"] == "https://minimax"

    @pytest.mark.parametrize(
        ("invalid_case", "error_match"),
        [
            ("unknown_target", "unknown skills"),
            ("missing_coverage", "beta"),
        ],
    )
    def test_task_preflight_fails_before_model_configuration(
        self,
        tmp_path,
        monkeypatch,
        invalid_case: str,
        error_match: str,
    ) -> None:
        alpha = _make_skill(tmp_path, "alpha")
        beta = _make_skill(tmp_path, "beta")
        train = [_task("train-a", ["alpha"])]
        val = [_task("val-a", ["alpha"]), _task("val-b", ["beta"])]
        if invalid_case == "unknown_target":
            train.append(_task("train-unknown", ["ghost"]))
        else:
            val.pop()
        split = _write_split(
            tmp_path,
            train,
            val,
            [_task("test-a", ["alpha"]), _task("test-b", ["beta"])],
        )
        cfg = _trainer_cfg(tmp_path, split, batch_size=1, eval_test=False)
        args = SimpleNamespace(
            config="unused.yaml",
            skill=[str(alpha), str(beta)],
            train_skill=[],
            out_root=str(tmp_path / "cli-out"),
        )
        configured: list[dict] = []
        monkeypatch.setattr(train_plugin, "parse_args", lambda: args)
        monkeypatch.setattr(train_plugin, "load_config", lambda _path: cfg)
        monkeypatch.setattr(train_plugin, "flatten_config", lambda structured: structured)
        monkeypatch.setattr(
            train_plugin,
            "_configure_models",
            lambda model_cfg: configured.append(model_cfg),
        )

        with pytest.raises(SystemExit, match=error_match):
            train_plugin.main()

        assert configured == []


class TestPluginTrainer:
    def test_preflight_rejects_zero_max_skills(self, tmp_path) -> None:
        state = collect_plugin_state(
            [
                str(_make_skill(tmp_path, "alpha")),
                str(_make_skill(tmp_path, "beta")),
            ]
        )
        split = _write_split(
            tmp_path,
            [_task("train-a", ["alpha"])],
            [_task("val-a", ["alpha"]), _task("val-b", ["beta"])],
            [],
        )
        cfg = _trainer_cfg(tmp_path, split, max_skills_per_candidate=0)
        adapter = SkillEvalAdapter(split_dir=str(split), split_mode="split_dir")

        with pytest.raises(ValueError, match="max_skills_per_candidate"):
            PluginTrainer(cfg, adapter, state).preflight()

    def test_accepts_complete_candidate_exports_and_resumes(
        self, tmp_path, monkeypatch
    ) -> None:
        state = collect_plugin_state(
            [
                str(_make_skill(tmp_path, "alpha", support=True)),
                str(_make_skill(tmp_path, "beta")),
            ]
        )
        split = _write_split(
            tmp_path,
            [_task("train-a", ["alpha"]), _task("train-b", ["beta"])],
            [_task("val-a", ["alpha"]), _task("val-b", ["beta"])],
            [_task("test-a", ["alpha"]), _task("test-b", ["beta"])],
        )
        cfg = _trainer_cfg(tmp_path, split)
        adapter = SkillEvalAdapter(
            split_dir=str(split),
            split_mode="split_dir",
            workers=1,
            timeout=60,
            analyst_workers=1,
            failure_only=True,
            minibatch_size=2,
            edit_budget=1,
        )
        reflect_calls: list[str] = []
        _patch_append_reflection(monkeypatch, adapter, reflect_calls)

        def fake_rollout(items, plugin_state, out_dir, **kwargs):
            del out_dir, kwargs
            results = []
            for item in items:
                targets = list(item.get("target_skills") or [])
                improved = bool(targets) and all(
                    "## Improved" in plugin_state.skill(name).content
                    for name in targets
                )
                results.append(
                    {
                        "id": item["id"],
                        "hard": int(improved),
                        "soft": float(improved),
                        "task_type": item.get("task_type", "default"),
                        "target_skills": targets,
                    }
                )
            return results

        monkeypatch.setattr(adapter, "rollout_plugin", fake_rollout)
        trainer = PluginTrainer(cfg, adapter, state)
        trainer.preflight()
        summary = trainer.train()

        assert summary["total_accepts"] == 1
        assert summary["best_selection_score"] == 1.0
        assert len(reflect_calls) == 2
        best = load_plugin_snapshot(str(tmp_path / "out" / "best_plugin"))
        assert "## Improved" in best.skill("alpha").content
        assert "## Improved" in best.skill("beta").content
        assert (
            tmp_path / "out" / "best_plugin" / "alpha" / "references" / "rules.md"
        ).is_file()

        resumed_adapter = SkillEvalAdapter(
            split_dir=str(split),
            split_mode="split_dir",
            workers=1,
            timeout=60,
            analyst_workers=1,
            failure_only=True,
            minibatch_size=2,
            edit_budget=1,
        )
        _patch_append_reflection(monkeypatch, resumed_adapter, reflect_calls)
        monkeypatch.setattr(resumed_adapter, "rollout_plugin", fake_rollout)
        resumed = PluginTrainer(cfg, resumed_adapter, state)
        resumed.preflight()
        resumed_summary = resumed.train()
        assert resumed_summary["total_steps"] == 1
        assert len(reflect_calls) == 2

    def test_rejects_overall_gain_when_other_skill_regresses(
        self, tmp_path, monkeypatch
    ) -> None:
        state = collect_plugin_state(
            [
                str(_make_skill(tmp_path, "alpha")),
                str(_make_skill(tmp_path, "beta")),
            ],
            ["alpha"],
        )
        split = _write_split(
            tmp_path,
            [_task("train-a", ["alpha"])],
            [
                _task("val-a", ["alpha"]),
                _task("val-b", ["beta"]),
                _task("val-general", None),
            ],
            [_task("test-a", ["alpha"]), _task("test-b", ["beta"])],
        )
        cfg = _trainer_cfg(
            tmp_path,
            split,
            batch_size=1,
            max_skills_per_candidate=1,
            eval_test=False,
        )
        adapter = SkillEvalAdapter(
            split_dir=str(split),
            split_mode="split_dir",
            workers=1,
            timeout=60,
            analyst_workers=1,
            failure_only=True,
            minibatch_size=1,
            edit_budget=1,
        )
        reflect_calls: list[str] = []
        _patch_append_reflection(monkeypatch, adapter, reflect_calls)

        def fake_rollout(items, plugin_state, out_dir, **kwargs):
            del out_dir, kwargs
            changed = "## Improved" in plugin_state.skill("alpha").content
            results = []
            for item in items:
                if item["id"] == "val-a":
                    hard = int(changed)
                elif item["id"] == "val-b":
                    hard = int(not changed)
                elif item["id"] == "val-general":
                    hard = int(changed)
                else:
                    hard = 0
                results.append(
                    {
                        "id": item["id"],
                        "hard": hard,
                        "soft": float(hard),
                        "task_type": item.get("task_type", "default"),
                        "target_skills": list(item.get("target_skills") or []),
                    }
                )
            return results

        monkeypatch.setattr(adapter, "rollout_plugin", fake_rollout)
        trainer = PluginTrainer(cfg, adapter, state)
        trainer.preflight()
        summary = trainer.train()

        assert summary["total_rejects"] == 1
        history = json.loads(
            (tmp_path / "out" / "history.json").read_text(encoding="utf-8")
        )
        assert history[0]["action"] == "reject"
        assert history[0]["candidate_score"] == pytest.approx(2 / 3)
        assert history[0]["regressions"]["beta"] == 1.0
        best = load_plugin_snapshot(str(tmp_path / "out" / "best_plugin"))
        assert "## Improved" not in best.skill("alpha").content
        runtime = json.loads(
            (tmp_path / "out" / "runtime_state.json").read_text(encoding="utf-8")
        )
        assert runtime["current_snapshot"].endswith(
            os.path.join("plugin_versions", "plugin_v0000")
        )
