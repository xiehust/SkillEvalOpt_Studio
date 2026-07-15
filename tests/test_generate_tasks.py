"""Tests for scripts/generate_tasks.py — prompt building, backend dispatch,
validate-retry loop.  No model calls: the exec harness run_* functions are
monkeypatched with fakes that write files the way a real agent would.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from skillopt.envs.skilleval.dataloader import load_tasks

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_tasks.py"
_spec = importlib.util.spec_from_file_location("generate_tasks_script", _SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


VALID_ITEMS = [
    {"id": "task_001", "question": "Do the thing?", "rubric": "Output contains DONE."},
    {"id": "task_002", "question": "Do the other thing?", "rubric": "Output contains OK."},
]
INVALID_ITEMS = [{"id": "task_001", "question": "No rubric here."}]
MULTI_ITEMS = [
    {
        "id": "task_001",
        "question": "Initialize a knowledge workspace.",
        "rubric": "The workspace is initialized.",
        "task_type": "skill-a",
        "target_skills": ["skill-a"],
    },
    {
        "id": "task_002",
        "question": "Report the current knowledge workspace status.",
        "rubric": "The response reports topic counts.",
        "task_type": "skill-b",
        "target_skills": ["skill-b"],
    },
]


class TestBuildPrompt:
    def test_contains_schema_count_and_rules(self):
        prompt = gen.build_prompt("# My skill\nAlways do X.", [], 7)
        assert "exactly 7" in prompt
        assert '"rubric"' in prompt and '"question"' in prompt and '"id"' in prompt
        assert "filesystem-safe" in prompt
        assert gen.OUTPUT_FILENAME in prompt
        assert "objectively checkable" in prompt
        assert "# My skill" in prompt

    def test_guidance_and_support_files(self):
        prompt = gen.build_prompt("skill", ["scripts/run.py"], 3, guidance="侧重边界场景")
        assert "侧重边界场景" in prompt
        assert "scripts/run.py" in prompt

    def test_feedback_appended_on_retry(self):
        prompt = gen.build_prompt("skill", [], 3, feedback="item #0: missing 'rubric'")
        assert "Previous attempt failed validation" in prompt
        assert "item #0: missing 'rubric'" in prompt

    def test_long_skill_truncated(self):
        prompt = gen.build_prompt("x" * (gen.MAX_SKILL_CHARS + 5000), [], 3)
        assert "[... skill truncated for prompt ...]" in prompt
        assert len(prompt) < gen.MAX_SKILL_CHARS + 4000

    def test_multi_skill_prompt_requires_targets_and_balanced_coverage(self):
        skills = [
            gen.SkillDocument("skill-a", "/a", "# A", ["references/a.md"]),
            gen.SkillDocument("skill-b", "/b", "# B", []),
        ]
        prompt = gen.build_multi_skill_prompt(skills, 6)
        assert "one unified evaluation task set" in prompt
        assert "### Skill: skill-a" in prompt
        assert "### Skill: skill-b" in prompt
        assert '"target_skills"' in prompt
        assert "Every skill must appear in target_skills for at least 1 distinct task" in prompt
        assert "routing/disambiguation" in prompt

    def test_multi_skill_prompt_includes_strict_per_skill_quota(self):
        skills = [
            gen.SkillDocument("skill-a", "/a", "# A", []),
            gen.SkillDocument("skill-b", "/b", "# B", []),
        ]
        prompt = gen.build_multi_skill_prompt(
            skills,
            5,
            min_tasks_per_skill=2,
        )
        assert "at least 2 distinct tasks" in prompt


class TestValidateGeneratedTasks:
    skills = [
        gen.SkillDocument("skill-a", "/a", "# A", []),
        gen.SkillDocument("skill-b", "/b", "# B", []),
    ]

    def test_multi_skill_valid(self):
        gen.validate_generated_tasks(MULTI_ITEMS, 2, self.skills)

    def test_multi_skill_rejects_unknown_target(self):
        items = [dict(MULTI_ITEMS[0], target_skills=["ghost"]), MULTI_ITEMS[1]]
        with pytest.raises(ValueError, match="unknown target_skills"):
            gen.validate_generated_tasks(items, 2, self.skills)

    def test_multi_skill_requires_full_coverage_when_count_allows(self):
        items = [MULTI_ITEMS[0], dict(MULTI_ITEMS[0], id="task_002")]
        with pytest.raises(
            ValueError,
            match=r"insufficient per-Skill coverage: skill-b=0/1",
        ):
            gen.validate_generated_tasks(items, 2, self.skills)

    def test_multi_skill_reports_actual_and_required_quota(self):
        items = [
            MULTI_ITEMS[0],
            dict(MULTI_ITEMS[0], id="task_002"),
            dict(MULTI_ITEMS[1], id="task_003"),
        ]
        with pytest.raises(ValueError, match=r"skill-b=1/2"):
            gen.validate_generated_tasks(
                items,
                3,
                self.skills,
                min_tasks_per_skill=2,
            )

    def test_generated_count_must_match_request(self):
        with pytest.raises(ValueError, match="expected exactly 3"):
            gen.validate_generated_tasks(MULTI_ITEMS, 3, self.skills)


def _fake_writer(items_per_call: list[list[dict]], calls: list[dict]):
    """Fake run_* that logs kwargs and writes the next scripted file content."""

    def fake(**kwargs):
        calls.append(kwargs)
        items = items_per_call[min(len(calls) - 1, len(items_per_call) - 1)]
        out = os.path.join(kwargs["work_dir"], gen.OUTPUT_FILENAME)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(items, f)
        return "wrote the file", "raw"

    return fake


def _run_main(monkeypatch, tmp_path: Path, extra_argv: list[str]) -> Path:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# S\nDo things carefully.\n", encoding="utf-8")
    out_root = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv",
        ["generate_tasks.py", "--skill", str(skill), "--out_root", str(out_root)] + extra_argv,
    )
    gen.main()
    return out_root


def _run_multi_main(monkeypatch, tmp_path: Path, extra_argv: list[str]) -> Path:
    skill_a = tmp_path / "skill-a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(
        "---\nname: skill-a\n---\n# A\n", encoding="utf-8"
    )
    skill_b = tmp_path / "skill-b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text(
        "---\nname: skill-b\n---\n# B\n", encoding="utf-8"
    )
    out_root = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_tasks.py",
            "--skill",
            str(skill_a),
            "--skill",
            str(skill_b),
            "--out_root",
            str(out_root),
        ]
        + extra_argv,
    )
    gen.main()
    return out_root


class TestMainDispatch:
    def test_claude_dispatch_absolute_workdir_and_default_model(self, monkeypatch, tmp_path):
        calls: list[dict] = []
        monkeypatch.setattr(gen, "run_claude_code_exec", _fake_writer([VALID_ITEMS], calls))
        out_root = _run_main(monkeypatch, tmp_path, ["--count", "2"])
        assert len(calls) == 1
        assert os.path.isabs(calls[0]["work_dir"])
        assert calls[0]["model"] == gen.default_model_for_backend("claude_code_exec")
        assert calls[0]["allow_file_edits"] is True
        tasks = load_tasks(str(out_root / "generated_tasks.json"))
        assert [t["id"] for t in tasks] == ["task_001", "task_002"]

    def test_codex_dispatch_keeps_model_empty(self, monkeypatch, tmp_path):
        calls: list[dict] = []
        monkeypatch.setattr(gen, "run_codex_exec", _fake_writer([VALID_ITEMS], calls))
        _run_main(monkeypatch, tmp_path, ["--backend", "codex_exec", "--count", "2"])
        assert len(calls) == 1
        assert calls[0]["model"] == ""  # codex CLI applies its own configured default

    def test_retry_with_feedback_then_success(self, monkeypatch, tmp_path):
        calls: list[dict] = []
        monkeypatch.setattr(
            gen, "run_claude_code_exec", _fake_writer([INVALID_ITEMS, VALID_ITEMS], calls)
        )
        out_root = _run_main(monkeypatch, tmp_path, ["--count", "2"])
        assert len(calls) == 2
        assert "Previous attempt failed validation" not in calls[0]["prompt"]
        assert "Previous attempt failed validation" in calls[1]["prompt"]
        assert "rubric" in calls[1]["prompt"]
        workspace = out_root / "gen_workspace"
        assert (workspace / f"{gen.OUTPUT_FILENAME}.attempt1.invalid").is_file()
        summary = json.loads((out_root / "gen_summary.json").read_text(encoding="utf-8"))
        assert summary["attempts"] == 2
        assert summary["count"] == 2

    def test_two_failures_exit_nonzero(self, monkeypatch, tmp_path):
        calls: list[dict] = []
        monkeypatch.setattr(
            gen, "run_claude_code_exec", _fake_writer([INVALID_ITEMS, INVALID_ITEMS], calls)
        )
        with pytest.raises(SystemExit) as excinfo:
            _run_main(monkeypatch, tmp_path, ["--count", "2"])
        assert "failed validation after 2 attempts" in str(excinfo.value)
        assert len(calls) == 2

    def test_agent_writes_nothing_is_fed_back(self, monkeypatch, tmp_path):
        calls: list[dict] = []

        def lazy_agent(**kwargs):
            calls.append(kwargs)
            return "I forgot to write the file", "raw"

        monkeypatch.setattr(gen, "run_claude_code_exec", lazy_agent)
        with pytest.raises(SystemExit) as excinfo:
            _run_main(monkeypatch, tmp_path, ["--count", "2"])
        assert "did not write" in str(excinfo.value)
        assert "did not write" in calls[1]["prompt"]

    def test_summary_records_model_and_backend(self, monkeypatch, tmp_path):
        calls: list[dict] = []
        monkeypatch.setattr(gen, "run_claude_code_exec", _fake_writer([VALID_ITEMS], calls))
        out_root = _run_main(monkeypatch, tmp_path, ["--count", "2", "--model", "my-model"])
        summary = json.loads((out_root / "gen_summary.json").read_text(encoding="utf-8"))
        assert summary["backend"] == "claude_code_exec"
        assert summary["model"] == "my-model"
        assert calls[0]["model"] == "my-model"

    def test_multi_skill_dispatch_and_summary(self, monkeypatch, tmp_path):
        calls: list[dict] = []
        monkeypatch.setattr(gen, "run_claude_code_exec", _fake_writer([MULTI_ITEMS], calls))
        out_root = _run_multi_main(monkeypatch, tmp_path, ["--count", "2"])
        assert len(calls) == 1
        assert "### Skill: skill-a" in calls[0]["prompt"]
        assert "### Skill: skill-b" in calls[0]["prompt"]
        summary = json.loads((out_root / "gen_summary.json").read_text(encoding="utf-8"))
        assert summary["skill"] is None
        assert summary["skill_names"] == ["skill-a", "skill-b"]
        assert summary["skill_count"] == 2
        assert len(summary["skills"]) == 2
        assert summary["min_tasks_per_skill"] == 1

    def test_multi_skill_quota_failure_is_retried_with_details(
        self,
        monkeypatch,
        tmp_path,
    ):
        deficient = [
            MULTI_ITEMS[0],
            dict(MULTI_ITEMS[0], id="task_002"),
            dict(MULTI_ITEMS[0], id="task_003"),
            dict(MULTI_ITEMS[0], id="task_004"),
        ]
        sufficient = [
            MULTI_ITEMS[0],
            dict(MULTI_ITEMS[0], id="task_002"),
            dict(MULTI_ITEMS[1], id="task_003"),
            dict(MULTI_ITEMS[1], id="task_004"),
        ]
        calls: list[dict] = []
        monkeypatch.setattr(
            gen,
            "run_claude_code_exec",
            _fake_writer([deficient, sufficient], calls),
        )

        out_root = _run_multi_main(
            monkeypatch,
            tmp_path,
            ["--count", "4", "--min-tasks-per-skill", "2"],
        )

        assert len(calls) == 2
        assert "skill-b=0/2" in calls[1]["prompt"]
        summary = json.loads(
            (out_root / "gen_summary.json").read_text(encoding="utf-8")
        )
        assert summary["min_tasks_per_skill"] == 2

    def test_multi_skill_quota_failure_twice_exits_nonzero(
        self,
        monkeypatch,
        tmp_path,
    ):
        deficient = [
            MULTI_ITEMS[0],
            dict(MULTI_ITEMS[0], id="task_002"),
        ]
        calls: list[dict] = []
        monkeypatch.setattr(
            gen,
            "run_claude_code_exec",
            _fake_writer([deficient, deficient], calls),
        )

        with pytest.raises(SystemExit, match="skill-b=0/2"):
            _run_multi_main(
                monkeypatch,
                tmp_path,
                ["--count", "2", "--min-tasks-per-skill", "2"],
            )
