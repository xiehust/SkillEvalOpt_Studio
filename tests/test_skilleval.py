"""Tests for the skilleval environment (custom skill evaluation)."""
from __future__ import annotations

import json
import os

import pytest

from skillopt.envs.skilleval.dataloader import load_tasks


def _write_tasks(tmp_path, items, name="tasks.json"):
    path = tmp_path / name
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _valid_item(task_id="task_001", **overrides):
    item = {
        "id": task_id,
        "question": "Summarize data/report.csv into a monthly table",
        "rubric": "Output must contain 12 month rows with correct sums",
    }
    item.update(overrides)
    return item


class TestLoadTasks:
    def test_happy_path_json_array(self, tmp_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item(), _valid_item("task_002")])
        tasks = load_tasks(path)
        assert [t["id"] for t in tasks] == ["task_001", "task_002"]

    def test_happy_path_jsonl(self, tmp_path) -> None:
        path = tmp_path / "tasks.jsonl"
        lines = [json.dumps(_valid_item(f"t{i}")) for i in range(3)]
        path.write_text("\n".join(lines), encoding="utf-8")
        tasks = load_tasks(str(path))
        assert [t["id"] for t in tasks] == ["t0", "t1", "t2"]

    @pytest.mark.parametrize("missing_field", ["id", "question", "rubric"])
    def test_missing_required_field_raises(self, tmp_path, missing_field) -> None:
        item = _valid_item()
        del item[missing_field]
        path = _write_tasks(tmp_path, [_valid_item("ok_task"), item])
        with pytest.raises(ValueError, match=f"item #1.*{missing_field}"):
            load_tasks(path)

    def test_empty_required_field_raises(self, tmp_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item(rubric="   ")])
        with pytest.raises(ValueError, match="rubric"):
            load_tasks(path)

    def test_error_message_names_index_and_id(self, tmp_path) -> None:
        item = _valid_item("bad_one")
        del item["question"]
        path = _write_tasks(tmp_path, [_valid_item(), item])
        with pytest.raises(ValueError) as excinfo:
            load_tasks(path)
        message = str(excinfo.value)
        assert "item #1" in message
        assert "bad_one" in message

    def test_duplicate_id_raises(self, tmp_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item("dup"), _valid_item("dup")])
        with pytest.raises(ValueError, match="duplicate id 'dup'"):
            load_tasks(path)

    @pytest.mark.parametrize("bad_id", ["a/b", "a\\b", "..", "x..y"])
    def test_unsafe_id_raises(self, tmp_path, bad_id) -> None:
        path = _write_tasks(tmp_path, [_valid_item(bad_id)])
        with pytest.raises(ValueError, match="filesystem-safe"):
            load_tasks(path)

    def test_non_str_files_value_raises(self, tmp_path) -> None:
        path = _write_tasks(
            tmp_path, [_valid_item(files={"data.csv": {"nested": "no"}})]
        )
        with pytest.raises(ValueError, match="'files' value.*must be str"):
            load_tasks(path)

    def test_non_dict_files_raises(self, tmp_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item(files=["a.txt"])])
        with pytest.raises(ValueError, match="'files' must be a dict"):
            load_tasks(path)

    def test_limit_truncates_after_full_validation(self, tmp_path) -> None:
        bad = _valid_item("late_bad")
        del bad["rubric"]
        items = [_valid_item(f"t{i}") for i in range(5)] + [bad]
        path = _write_tasks(tmp_path, items)
        # corrupt item beyond the limit still fails the whole file
        with pytest.raises(ValueError, match="late_bad"):
            load_tasks(path, limit=2)

    def test_limit_returns_first_n(self, tmp_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item(f"t{i}") for i in range(5)])
        tasks = load_tasks(path, limit=2)
        assert [t["id"] for t in tasks] == ["t0", "t1"]

    def test_normalization_defaults(self, tmp_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item()])
        task = load_tasks(path)[0]
        assert task["task_type"] == "default"
        assert task["files"] == {}

    def test_normalization_preserves_values(self, tmp_path) -> None:
        path = _write_tasks(
            tmp_path,
            [_valid_item(task_type="qa", files={"a.txt": "hello"})],
        )
        task = load_tasks(path)[0]
        assert task["task_type"] == "qa"
        assert task["files"] == {"a.txt": "hello"}

    def test_does_not_mutate_caller_visible_structures(self, tmp_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item()])
        first = load_tasks(path)[0]
        first["task_type"] = "mutated"
        again = load_tasks(path)[0]
        assert again["task_type"] == "default"

    def test_empty_file_raises(self, tmp_path) -> None:
        path = tmp_path / "empty.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="No task items"):
            load_tasks(str(path))


# ── Judge / evaluator ─────────────────────────────────────────────────────

from skillopt.envs.skilleval import evaluator  # noqa: E402
from skillopt.envs.skilleval.evaluator import _extract_verdict, judge  # noqa: E402

_VERDICT_JSON = '{"pass": true, "score": 0.9, "reason": "meets rubric"}'


class _FakeOptimizer:
    """Callable stand-in for chat_optimizer recording calls."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, *, system, user, stage=""):
        self.calls.append({"system": system, "user": user, "stage": stage})
        return self.replies.pop(0), {}


class TestExtractVerdict:
    def test_clean_json(self) -> None:
        verdict = _extract_verdict(_VERDICT_JSON)
        assert verdict == {"pass": True, "score": 0.9, "reason": "meets rubric"}

    def test_fenced_json(self) -> None:
        text = f"```json\n{_VERDICT_JSON}\n```"
        verdict = _extract_verdict(text)
        assert verdict is not None
        assert verdict["pass"] is True
        assert verdict["score"] == 0.9

    def test_prose_embedded_json(self) -> None:
        text = f"Here is my assessment.\n{_VERDICT_JSON}\nHope that helps!"
        verdict = _extract_verdict(text)
        assert verdict is not None
        assert verdict["reason"] == "meets rubric"

    @pytest.mark.parametrize(
        ("raw", "expected"), [(1.5, 1.0), (-0.2, 0.0), (0.5, 0.5)]
    )
    def test_score_clamped(self, raw, expected) -> None:
        verdict = _extract_verdict(
            json.dumps({"pass": False, "score": raw, "reason": ""})
        )
        assert verdict is not None
        assert verdict["score"] == expected

    @pytest.mark.parametrize(
        "text", ["", "not json", '{"score": 0.5}', '{"pass": true, "score": "high"}']
    )
    def test_unrecoverable_returns_none(self, text) -> None:
        assert _extract_verdict(text) is None


class TestJudge:
    def _item(self):
        return _valid_item()

    def test_clean_verdict(self, monkeypatch) -> None:
        fake = _FakeOptimizer([_VERDICT_JSON])
        monkeypatch.setattr(evaluator, "chat_optimizer", fake)
        result = judge(self._item(), "the answer", "out.csv (1.2K)")
        assert result == {
            "id": "task_001",
            "hard": 1,
            "soft": 0.9,
            "judge_reason": "meets rubric",
            "judge_usage": {"input": 0, "output": 0},
        }

    def test_prompt_contains_all_sections(self, monkeypatch) -> None:
        fake = _FakeOptimizer([_VERDICT_JSON])
        monkeypatch.setattr(evaluator, "chat_optimizer", fake)
        item = self._item()
        judge(item, "my answer", "out.csv (1.2K)")
        prompt = fake.calls[0]["user"]
        assert item["question"] in prompt
        assert item["rubric"] in prompt
        assert "my answer" in prompt
        assert "out.csv (1.2K)" in prompt

    def test_malformed_then_valid_retries_once(self, monkeypatch) -> None:
        fake = _FakeOptimizer(["not json at all", _VERDICT_JSON])
        monkeypatch.setattr(evaluator, "chat_optimizer", fake)
        result = judge(self._item(), "answer")
        assert len(fake.calls) == 2
        assert "not valid JSON" in fake.calls[1]["user"]
        assert result["hard"] == 1
        assert "judge_error" not in result

    def test_malformed_twice_sets_judge_error(self, monkeypatch) -> None:
        fake = _FakeOptimizer(["garbage", "more garbage"])
        monkeypatch.setattr(evaluator, "chat_optimizer", fake)
        result = judge(self._item(), "answer")
        assert result["hard"] == 0
        assert result["soft"] == 0.0
        assert "unparseable" in result["judge_error"]

    def test_optimizer_exception_never_raises(self, monkeypatch) -> None:
        def boom(**kwargs):
            raise RuntimeError("backend down")

        monkeypatch.setattr(evaluator, "chat_optimizer", boom)
        result = judge(self._item(), "answer")
        assert result["hard"] == 0
        assert "judge call failed" in result["judge_error"]

    def test_empty_response_short_circuits(self, monkeypatch) -> None:
        fake = _FakeOptimizer([])
        monkeypatch.setattr(evaluator, "chat_optimizer", fake)
        result = judge(self._item(), "   ")
        assert fake.calls == []
        assert result["judge_skipped"] == "empty_response"
        assert result["hard"] == 0
        assert result["soft"] == 0.0

    def test_false_pass_gives_hard_zero(self, monkeypatch) -> None:
        fake = _FakeOptimizer(
            ['{"pass": false, "score": 0.4, "reason": "partial"}']
        )
        monkeypatch.setattr(evaluator, "chat_optimizer", fake)
        result = judge(self._item(), "answer")
        assert result["hard"] == 0
        assert result["soft"] == 0.4


# ── Rollout ───────────────────────────────────────────────────────────────

import time  # noqa: E402

from skillopt.envs.skilleval import rollout as rollout_mod  # noqa: E402
from skillopt.envs.skilleval.rollout import GUIDE_PROMPT, run_batch  # noqa: E402


def _three_items():
    return [
        _valid_item("t1"),
        _valid_item("t2"),
        _valid_item("t3", task_type="qa"),
    ]


class TestRunBatch:
    def _patch_harness(self, monkeypatch, exec_fn):
        prepared = []

        def fake_prepare(**kwargs):
            prepared.append(kwargs)
            return "", ""

        monkeypatch.setattr(rollout_mod, "prepare_workspace", fake_prepare)
        monkeypatch.setattr(rollout_mod, "run_claude_code_exec", exec_fn)
        return prepared

    def test_order_preserved_despite_completion_order(self, tmp_path, monkeypatch) -> None:
        def slow_first(*, work_dir, prompt, model, timeout, **kw):
            # t1 finishes last; order must still follow input order
            if work_dir.endswith("t1"):
                time.sleep(0.05)
            return f"answer for {os.path.basename(work_dir)}", "raw"

        self._patch_harness(monkeypatch, slow_first)
        results = run_batch(_three_items(), "# skill", str(tmp_path), workers=3)
        assert [r["id"] for r in results] == ["t1", "t2", "t3"]
        assert results[0]["response"] == "answer for t1"

    def test_single_failure_is_isolated(self, tmp_path, monkeypatch) -> None:
        def explode_on_t2(*, work_dir, prompt, model, timeout, **kw):
            if work_dir.endswith("t2"):
                raise RuntimeError("CLI crashed")
            return "ok", "raw"

        self._patch_harness(monkeypatch, explode_on_t2)
        results = run_batch(_three_items(), "# skill", str(tmp_path), workers=2)
        assert [r["id"] for r in results] == ["t1", "t2", "t3"]
        assert results[0]["response"] == "ok"
        assert results[2]["response"] == "ok"
        assert results[1]["response"] == ""
        assert "RuntimeError: CLI crashed" in results[1]["error"]
        assert "error" not in results[0]

    def test_work_dir_shape_and_workspace_seeding(self, tmp_path, monkeypatch) -> None:
        prepared = self._patch_harness(
            monkeypatch, lambda **kw: ("ok", "raw")
        )
        items = [_valid_item("t1", files={"data.csv": "a,b"})]
        results = run_batch(items, "# my skill", str(tmp_path))
        expected_dir = str(tmp_path / "rollouts" / "t1")
        assert results[0]["work_dir"] == expected_dir
        assert prepared[0]["work_dir"] == expected_dir
        assert prepared[0]["skill_md"] == "# my skill"
        assert prepared[0]["task_text"] == items[0]["question"]
        assert prepared[0]["extra_files"] == {"data.csv": "a,b"}
        assert prepared[0]["copy_files"] is None

    def test_skill_files_copied_into_skill_dir(self, tmp_path, monkeypatch) -> None:
        prepared = self._patch_harness(monkeypatch, lambda **kw: ("ok", "raw"))
        skill_files = [
            ("/abs/skill/scripts/run.py", os.path.join("scripts", "run.py")),
            ("/abs/skill/references/doc.md", os.path.join("references", "doc.md")),
        ]
        run_batch([_valid_item("t1")], "# skill", str(tmp_path), skill_files=skill_files)
        copied = prepared[0]["copy_files"]
        assert copied == [
            ("/abs/skill/scripts/run.py",
             os.path.join(".agents", "skills", "skillopt-target", "scripts", "run.py")),
            ("/abs/skill/references/doc.md",
             os.path.join(".agents", "skills", "skillopt-target", "references", "doc.md")),
        ]

    def test_guide_prompt_mentions_skill_and_task(self) -> None:
        assert ".agents/skills/skillopt-target/SKILL.md" in GUIDE_PROMPT
        assert "task.md" in GUIDE_PROMPT

    def test_exec_allows_file_edits_and_write_tools(self, tmp_path, monkeypatch) -> None:
        seen = []

        def record_exec(**kw):
            seen.append(kw)
            return "ok", "raw"

        monkeypatch.setattr(rollout_mod, "prepare_workspace", lambda **kw: ("", ""))
        monkeypatch.setattr(rollout_mod, "run_claude_code_exec", record_exec)
        run_batch([_valid_item("t1")], "# skill", str(tmp_path))
        assert seen[0]["allow_file_edits"] is True
        assert "Write" in seen[0]["allowed_tools"]

    def test_duration_recorded(self, tmp_path, monkeypatch) -> None:
        self._patch_harness(monkeypatch, lambda **kw: ("ok", "raw"))
        results = run_batch(_three_items(), "# skill", str(tmp_path))
        assert all(r["duration_s"] >= 0 for r in results)

    def test_prepare_failure_is_isolated(self, tmp_path, monkeypatch) -> None:
        def bad_prepare(**kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(rollout_mod, "prepare_workspace", bad_prepare)
        monkeypatch.setattr(
            rollout_mod, "run_claude_code_exec", lambda **kw: ("ok", "raw")
        )
        results = run_batch([_valid_item("t1")], "# skill", str(tmp_path))
        assert "OSError: disk full" in results[0]["error"]
        assert results[0]["response"] == ""

    def test_empty_items_returns_empty(self, tmp_path) -> None:
        assert run_batch([], "# skill", str(tmp_path)) == []

    def test_codex_backend_dispatches_to_codex_exec(self, tmp_path, monkeypatch) -> None:
        claude_calls, codex_calls = [], []
        monkeypatch.setattr(rollout_mod, "prepare_workspace", lambda **kw: ("", ""))
        monkeypatch.setattr(
            rollout_mod, "run_claude_code_exec",
            lambda **kw: claude_calls.append(kw) or ("claude answer", "raw"),
        )
        monkeypatch.setattr(
            rollout_mod, "run_codex_exec",
            lambda **kw: codex_calls.append(kw) or ("codex answer", "raw"),
        )
        monkeypatch.setattr(rollout_mod, "get_target_backend", lambda: "codex_exec")

        results = run_batch([_valid_item("t1")], "# skill", str(tmp_path), model="gpt-5.5")
        assert results[0]["response"] == "codex answer"
        assert not claude_calls
        assert codex_calls[0]["model"] == "gpt-5.5"
        assert codex_calls[0]["prompt"] == rollout_mod.GUIDE_PROMPT
        assert os.path.isabs(codex_calls[0]["work_dir"])  # codex -C needs an absolute path

    def test_default_backend_still_uses_claude(self, tmp_path, monkeypatch) -> None:
        codex_calls = []
        self._patch_harness(monkeypatch, lambda **kw: ("claude answer", "raw"))
        monkeypatch.setattr(
            rollout_mod, "run_codex_exec",
            lambda **kw: codex_calls.append(kw) or ("codex answer", "raw"),
        )
        monkeypatch.setattr(rollout_mod, "get_target_backend", lambda: "claude_code_exec")
        results = run_batch([_valid_item("t1")], "# skill", str(tmp_path))
        assert results[0]["response"] == "claude answer"
        assert not codex_calls


# ── CLI / report ──────────────────────────────────────────────────────────

import importlib.util  # noqa: E402
import sys  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "evaluate_skill",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "evaluate_skill.py"),
)
evaluate_skill = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("evaluate_skill", evaluate_skill)
_SPEC.loader.exec_module(evaluate_skill)


def _result(task_id, hard, soft, task_type="default", **extra):
    base = {
        "id": task_id,
        "hard": hard,
        "soft": soft,
        "task_type": task_type,
        "judge_reason": extra.pop("judge_reason", "ok"),
        "duration_s": extra.pop("duration_s", 1.0),
    }
    base.update(extra)
    return base


class TestBuildReport:
    def test_summary_math(self) -> None:
        results = [
            _result("t1", 1, 0.9),
            _result("t2", 0, 0.5),
            _result("t3", 1, 1.0, task_type="qa"),
            _result("t4", 0, 0.0, task_type="qa"),
        ]
        report = evaluate_skill.build_report(results)
        assert "- Tasks: 4" in report
        assert "- Pass rate (hard): 50.0%" in report
        assert "- Soft score mean: 0.600" in report

    def test_task_type_grouping(self) -> None:
        results = [
            _result("t1", 1, 1.0, task_type="qa"),
            _result("t2", 0, 0.0, task_type="code"),
        ]
        report = evaluate_skill.build_report(results)
        assert "| qa | 1 | 100.0% | 1.000 |" in report
        assert "| code | 1 | 0.0% | 0.000 |" in report

    def test_failure_sections(self) -> None:
        results = [
            _result("t1", 0, 0.0, error="RuntimeError: crashed"),
            _result("t2", 0, 0.0, judge_error="unparseable judge reply"),
            _result("t3", 1, 1.0),
        ]
        report = evaluate_skill.build_report(results)
        assert "### Rollout errors" in report
        assert "`t1`: RuntimeError: crashed" in report
        assert "### Judge errors" in report
        assert "`t2`: unparseable judge reply" in report

    def test_no_failures_says_none(self) -> None:
        report = evaluate_skill.build_report([_result("t1", 1, 1.0)])
        assert "## Failures\n\nnone" in report

    def test_reason_truncated(self) -> None:
        long_reason = "x" * 200
        report = evaluate_skill.build_report(
            [_result("t1", 1, 1.0, judge_reason=long_reason)]
        )
        assert "x" * 77 + "..." in report
        assert long_reason not in report

    def test_cost_section(self) -> None:
        results = [
            _result("t1", 1, 1.0, duration_s=2.0),
            _result("t2", 1, 1.0, duration_s=4.0),
        ]
        report = evaluate_skill.build_report(results)
        assert "- Total duration: 6.0s" in report
        assert "- Mean duration per task: 3.0s" in report
        assert "Token usage: n/a" in report

    def test_empty_results_no_crash(self) -> None:
        report = evaluate_skill.build_report([])
        assert "- Tasks: 0" in report
        assert "- Pass rate (hard): 0.0%" in report

    def test_sample_report_printed(self, capsys) -> None:
        # evidence artifact: full sample report into the test output
        results = [
            _result("t1", 1, 0.9, judge_reason="meets rubric"),
            _result("t2", 0, 0.2, task_type="qa",
                    judge_reason="missing monthly totals"),
        ]
        print(evaluate_skill.build_report(results))
        captured = capsys.readouterr()
        assert "# Skill Evaluation Report" in captured.out


class TestMergeScores:
    def _items(self):
        return [_valid_item("t1"), _valid_item("t2")]

    def test_errored_task_skips_judge(self) -> None:
        rollouts = [
            {"id": "t1", "task_type": "default", "response": "",
             "error": "boom", "duration_s": 0.1, "work_dir": "/nonexistent/t1"},
            {"id": "t2", "task_type": "default", "response": "fine",
             "duration_s": 0.2, "work_dir": "/nonexistent/t2"},
        ]
        judged = []

        def fake_judge(item, response, listing):
            judged.append(item["id"])
            return {"id": item["id"], "hard": 1, "soft": 1.0,
                    "judge_reason": "ok"}

        merged = evaluate_skill.merge_scores(self._items(), rollouts, fake_judge)
        assert judged == ["t2"]
        assert merged[0]["hard"] == 0
        assert merged[0]["soft"] == 0.0
        assert merged[0]["error"] == "boom"
        assert merged[1]["hard"] == 1

    def test_merged_keeps_rollout_fields(self) -> None:
        rollouts = [
            {"id": "t1", "task_type": "default", "response": "answer",
             "duration_s": 3.5, "work_dir": "/nonexistent/t1"},
        ]

        def fake_judge(item, response, listing):
            return {"id": item["id"], "hard": 1, "soft": 0.8,
                    "judge_reason": "good"}

        merged = evaluate_skill.merge_scores(
            [_valid_item("t1")], rollouts, fake_judge
        )
        assert merged[0]["duration_s"] == 3.5
        assert merged[0]["response"] == "answer"
        assert merged[0]["soft"] == 0.8


class TestCollectSkill:
    def _make_skill_dir(self, tmp_path):
        skill = tmp_path / "my-skill"
        (skill / "scripts").mkdir(parents=True)
        (skill / "references").mkdir()
        (skill / ".git").mkdir()
        (skill / "__pycache__").mkdir()
        (skill / "SKILL.md").write_text("# My Skill", encoding="utf-8")
        (skill / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
        (skill / "references" / "doc.md").write_text("ref", encoding="utf-8")
        (skill / "LICENSE.txt").write_text("MIT", encoding="utf-8")
        (skill / ".hidden").write_text("x", encoding="utf-8")
        (skill / ".git" / "config").write_text("x", encoding="utf-8")
        (skill / "__pycache__" / "run.cpython-312.pyc").write_text("x", encoding="utf-8")
        return skill

    def test_directory_mode_collects_supporting_files(self, tmp_path) -> None:
        skill = self._make_skill_dir(tmp_path)
        content, files = evaluate_skill._collect_skill(str(skill))
        assert content == "# My Skill"
        rels = sorted(rel for _src, rel in files)
        assert rels == [
            "LICENSE.txt",
            os.path.join("references", "doc.md"),
            os.path.join("scripts", "run.py"),
        ]
        assert all(os.path.isabs(src) for src, _rel in files)

    def test_directory_without_skill_md_exits(self, tmp_path) -> None:
        empty = tmp_path / "not-a-skill"
        empty.mkdir()
        with pytest.raises(SystemExit, match="no SKILL.md"):
            evaluate_skill._collect_skill(str(empty))

    def test_file_mode_has_no_supporting_files(self, tmp_path) -> None:
        md = tmp_path / "SKILL.md"
        md.write_text("# solo", encoding="utf-8")
        content, files = evaluate_skill._collect_skill(str(md))
        assert content == "# solo"
        assert files == []

    def test_symlinked_file_is_skipped(self, tmp_path) -> None:
        skill = self._make_skill_dir(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (skill / "link.txt").symlink_to(outside)
        _content, files = evaluate_skill._collect_skill(str(skill))
        assert "link.txt" not in [rel for _src, rel in files]


# ── Training adapter ──────────────────────────────────────────────────────

from skillopt.envs.skilleval import adapter as adapter_mod  # noqa: E402
from skillopt.envs.skilleval.adapter import SkillEvalAdapter  # noqa: E402
from skillopt.envs.skilleval.dataloader import SkillEvalDataLoader  # noqa: E402


def _make_split_dir(tmp_path, counts=(2, 1, 1)):
    split = tmp_path / "split"
    idx = 0
    for name, n in zip(("train", "val", "test"), counts):
        d = split / name
        d.mkdir(parents=True)
        items = [_valid_item(f"t{idx + i}") for i in range(n)]
        idx += n
        (d / "items.json").write_text(json.dumps(items), encoding="utf-8")
    return str(split)


class TestSkillEvalDataLoader:
    def test_loads_and_validates_splits(self, tmp_path) -> None:
        loader = SkillEvalDataLoader(split_dir=_make_split_dir(tmp_path), split_mode="split_dir")
        loader.setup({})
        assert len(loader.train_items) == 2
        assert len(loader.val_items) == 1
        assert loader.train_items[0]["task_type"] == "default"

    def test_invalid_split_item_fails_fast(self, tmp_path) -> None:
        split = tmp_path / "split"
        for name in ("train", "val", "test"):
            d = split / name
            d.mkdir(parents=True)
            (d / "items.json").write_text(json.dumps([_valid_item("x" + name)]), encoding="utf-8")
        bad = _valid_item("bad")
        del bad["rubric"]
        (split / "train" / "items.json").write_text(json.dumps([bad]), encoding="utf-8")
        loader = SkillEvalDataLoader(split_dir=str(split), split_mode="split_dir")
        with pytest.raises(ValueError, match="rubric"):
            loader.setup({})


class TestSkillEvalAdapter:
    def _adapter(self, tmp_path):
        a = SkillEvalAdapter(split_dir=_make_split_dir(tmp_path), split_mode="split_dir")
        a.setup({})
        return a

    def test_rollout_merges_judge_and_persists_trajectories(self, tmp_path, monkeypatch) -> None:
        a = self._adapter(tmp_path)
        items = a.build_train_env(batch_size=2, seed=1)

        def fake_run_batch(batch_items, skill, out_dir, **kw):
            return [
                {"id": batch_items[0]["id"], "task_type": "default",
                 "response": "answer A", "duration_s": 1.0, "work_dir": "/nx/a"},
                {"id": batch_items[1]["id"], "task_type": "default",
                 "response": "", "error": "boom", "duration_s": 0.1, "work_dir": "/nx/b"},
            ]

        def fake_judge(item, response, listing):
            return {"id": item["id"], "hard": 1, "soft": 0.75, "judge_reason": "ok"}

        monkeypatch.setattr(adapter_mod, "run_batch", fake_run_batch)
        monkeypatch.setattr(adapter_mod, "judge", fake_judge)

        out_dir = str(tmp_path / "rollout")
        results = a.rollout(items, "# skill", out_dir)

        assert results[0]["hard"] == 1 and results[0]["soft"] == 0.75
        assert results[1]["hard"] == 0 and "error" in results[1]
        # reflection contract: conversation.json per task + enriched fields
        for r, item in zip(results, items):
            conv = json.loads(
                (tmp_path / "rollout" / "predictions" / r["id"] / "conversation.json").read_text()
            )
            assert conv[0]["content"] == item["question"]
            assert "Judge verdict" in conv[2]["content"]
        assert results[1]["fail_reason"]
        assert results[0]["task_description"] == items[0]["question"]

    def test_reference_text_exposes_rubric(self, tmp_path) -> None:
        a = self._adapter(tmp_path)
        ref = a.build_reference_text(_valid_item())
        assert "rubric" in ref.lower()
        assert "12 month rows" in ref

    def test_task_types_collected(self, tmp_path) -> None:
        a = self._adapter(tmp_path)
        assert a.get_task_types() == ["default"]


# ── collect_support_files + adapter skill_dir (multi-file skill training) ──
from skillopt.envs.skilleval.rollout import collect_support_files  # noqa: E402


def _make_skill_dir(tmp_path):
    skill = tmp_path / "myskill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# skill", encoding="utf-8")
    (skill / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
    (skill / "notes.md").write_text("notes", encoding="utf-8")
    return skill


class TestCollectSupportFiles:
    def test_collects_nested_files_with_relative_paths(self, tmp_path) -> None:
        skill = _make_skill_dir(tmp_path)
        files = collect_support_files(str(skill))
        rels = sorted(rel for _, rel in files)
        assert rels == ["notes.md", os.path.join("scripts", "run.py")]
        assert all(os.path.isabs(src) for src, _ in files)

    def test_skips_skill_md_hidden_caches_and_symlinks(self, tmp_path) -> None:
        skill = _make_skill_dir(tmp_path)
        (skill / ".hidden").write_text("x", encoding="utf-8")
        (skill / "__pycache__").mkdir()
        (skill / "__pycache__" / "c.pyc").write_text("x", encoding="utf-8")
        os.symlink(str(skill / "notes.md"), str(skill / "link.md"))
        rels = {rel for _, rel in collect_support_files(str(skill))}
        assert rels == {"notes.md", os.path.join("scripts", "run.py")}

    def test_non_directory_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="skill_dir"):
            collect_support_files(str(tmp_path / "nx"))


class TestSkillEvalAdapterSkillDir:
    def test_skill_dir_files_passed_to_run_batch(self, tmp_path, monkeypatch) -> None:
        skill = _make_skill_dir(tmp_path)
        a = SkillEvalAdapter(
            split_dir=_make_split_dir(tmp_path), split_mode="split_dir",
            skill_dir=str(skill),
        )
        a.setup({})
        items = a.build_train_env(batch_size=2, seed=1)
        captured = {}

        def fake_run_batch(batch_items, skill_content, out_dir, **kw):
            captured.update(kw)
            return [{"id": it["id"], "task_type": "default", "response": "r",
                     "duration_s": 0.1, "work_dir": "/nx"} for it in batch_items]

        monkeypatch.setattr(adapter_mod, "run_batch", fake_run_batch)
        monkeypatch.setattr(
            adapter_mod, "judge",
            lambda item, response, listing: {"id": item["id"], "hard": 1, "soft": 1.0},
        )
        a.rollout(items, "# skill", str(tmp_path / "out"))
        rels = sorted(rel for _, rel in captured["skill_files"])
        assert rels == ["notes.md", os.path.join("scripts", "run.py")]

    def test_no_skill_dir_passes_none(self, tmp_path) -> None:
        a = SkillEvalAdapter(split_dir=_make_split_dir(tmp_path), split_mode="split_dir")
        assert a.skill_files is None

    def test_bad_skill_dir_fails_fast_at_construction(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="skill_dir"):
            SkillEvalAdapter(
                split_dir=_make_split_dir(tmp_path), split_mode="split_dir",
                skill_dir=str(tmp_path / "nx"),
            )


# ── artifacts_excerpts (judge sees produced text-file contents) ───────────
from skillopt.envs.skilleval.evaluator import artifacts_excerpts, merge_scores  # noqa: E402


def _make_work_dir(tmp_path):
    wd = tmp_path / "wd"
    (wd / "triage").mkdir(parents=True)
    (wd / "logs").mkdir()
    (wd / ".agents" / "skills").mkdir(parents=True)
    (wd / "task.md").write_text("the task", encoding="utf-8")
    (wd / "logs" / "access.log").write_text("seeded input", encoding="utf-8")
    (wd / "triage" / "report.md").write_text("# Triage\nSTATUS: OK", encoding="utf-8")
    (wd / ".agents" / "skills" / "SKILL.md").write_text("skill", encoding="utf-8")
    return wd


class TestArtifactsExcerpts:
    def test_includes_produced_text_excludes_seeded_and_internal(self, tmp_path) -> None:
        wd = _make_work_dir(tmp_path)
        out = artifacts_excerpts(str(wd), exclude_rel=["logs/access.log"])
        assert "triage/report.md" in out.replace(os.sep, "/")
        assert "STATUS: OK" in out
        assert "seeded input" not in out
        assert "the task" not in out
        assert "SKILL.md" not in out

    def test_binary_files_skipped(self, tmp_path) -> None:
        wd = _make_work_dir(tmp_path)
        (wd / "out.xlsx").write_bytes(b"PK\x03\x04\x00\x00binary")
        out = artifacts_excerpts(str(wd))
        assert "out.xlsx" not in out

    def test_truncation_is_marked(self, tmp_path) -> None:
        wd = _make_work_dir(tmp_path)
        (wd / "big.md").write_text("x" * 5000, encoding="utf-8")
        out = artifacts_excerpts(str(wd), per_file_chars=100)
        assert "truncated: first 100 chars of 5000 bytes" in out

    def test_file_cap_is_reported_not_silent(self, tmp_path) -> None:
        wd = _make_work_dir(tmp_path)
        for i in range(4):
            (wd / f"f{i}.txt").write_text("hi", encoding="utf-8")
        out = artifacts_excerpts(str(wd), max_files=2)
        assert "more file(s) not shown" in out

    def test_missing_dir_returns_empty(self, tmp_path) -> None:
        assert artifacts_excerpts(str(tmp_path / "nx")) == ""

    def test_merge_scores_feeds_contents_to_judge(self, tmp_path) -> None:
        wd = _make_work_dir(tmp_path)
        item = {"id": "t1", "question": "q", "rubric": "r",
                "files": {"logs/access.log": "seeded input"}}
        rollout = {"id": "t1", "response": "done", "work_dir": str(wd)}
        seen = {}

        def fake_judge(itm, response, listing):
            seen["listing"] = listing
            return {"id": itm["id"], "hard": 1, "soft": 1.0, "judge_reason": "ok"}

        merge_scores([item], [rollout], fake_judge)
        assert "STATUS: OK" in seen["listing"]
        assert "seeded input" not in seen["listing"]


# ── bundle codec + trainable_files (multi-doc skill training) ──────────────
from skillopt.envs.skilleval import bundle as bundle_mod  # noqa: E402
from skillopt.envs.skilleval.bundle import build_bundle, is_bundle, split_bundle  # noqa: E402


class TestBundleCodec:
    def test_round_trip_and_skill_md_last(self) -> None:
        text = build_bundle("# skill", [("references/a.md", "AAA"), ("references/b.md", "BBB")])
        assert text.rstrip().endswith("# skill")
        assert text.index("references/a.md") < text.index("references/b.md") < text.index("SKILL.md")
        docs = split_bundle(text)
        assert docs == {"references/a.md": "AAA", "references/b.md": "BBB", "SKILL.md": "# skill"}

    def test_no_headers_means_single_doc_skill(self) -> None:
        assert split_bundle("# plain skill") == {"SKILL.md": "# plain skill"}
        assert not is_bundle("# plain skill")
        assert is_bundle(build_bundle("# s", []))

    def test_leading_text_attaches_to_first_section(self) -> None:
        text = "stray edit\n" + build_bundle("# skill", [("a.md", "AAA")])
        docs = split_bundle(text)
        assert docs["a.md"] == "stray edit\nAAA"
        assert docs["SKILL.md"] == "# skill"

    def test_sections_outside_whitelist_are_dropped(self) -> None:
        text = build_bundle("# skill", [("a.md", "AAA"), ("evil.md", "EEE")])
        docs = split_bundle(text, allowed=["a.md"])
        assert set(docs) == {"a.md", "SKILL.md"}

    def test_unsafe_paths_are_dropped_or_rejected(self) -> None:
        docs = split_bundle("<!-- FILE: ../escape.md -->\nX\n<!-- FILE: SKILL.md -->\n# s")
        assert set(docs) == {"SKILL.md"}
        with pytest.raises(ValueError):
            build_bundle("# s", [("/abs/path.md", "X")])
        with pytest.raises(ValueError):
            build_bundle("# s", [("SKILL.md", "X")])

    def test_repeated_path_keeps_last(self) -> None:
        text = ("<!-- FILE: a.md -->\nold\n<!-- FILE: a.md -->\nnew\n"
                "<!-- FILE: SKILL.md -->\n# s")
        assert split_bundle(text)["a.md"] == "new"

    def test_cli_build_and_split_round_trip(self, tmp_path, monkeypatch, capsys) -> None:
        skill = _make_skill_dir(tmp_path)
        out_bundle = tmp_path / "seed.md"
        monkeypatch.setattr("sys.argv", ["bundle", "build", str(skill),
                                         "--files", "notes.md", "--out", str(out_bundle)])
        bundle_mod.main()
        assert "notes.md" in out_bundle.read_text(encoding="utf-8")
        out_dir = tmp_path / "deploy"
        monkeypatch.setattr("sys.argv", ["bundle", "split", str(out_bundle),
                                         "--skill_dir", str(skill), "--out_dir", str(out_dir)])
        bundle_mod.main()
        assert (out_dir / "SKILL.md").read_text(encoding="utf-8").strip() == "# skill"
        assert (out_dir / "notes.md").read_text(encoding="utf-8").strip() == "notes"
        assert (out_dir / "scripts" / "run.py").is_file()  # frozen file copied


class TestRunBatchSkillDocs:
    def test_skill_docs_written_into_install_dir(self, tmp_path, monkeypatch) -> None:
        prepared = []
        monkeypatch.setattr(rollout_mod, "prepare_workspace",
                            lambda **kw: prepared.append(kw) or ("", ""))
        monkeypatch.setattr(rollout_mod, "run_claude_code_exec", lambda **kw: ("ok", "raw"))
        items = [_valid_item("t1", files={"data.csv": "a,b"})]
        run_batch(items, "# skill", str(tmp_path),
                  skill_docs={"references/tpl.md": "TPL"})
        extra = prepared[0]["extra_files"]
        assert extra["data.csv"] == "a,b"
        key = os.path.join(".agents", "skills", "skillopt-target", "references", "tpl.md")
        assert extra[key] == "TPL"


class TestSkillEvalAdapterTrainableFiles:
    def _skill_dir(self, tmp_path):
        skill = tmp_path / "mskill"
        (skill / "references").mkdir(parents=True)
        (skill / "scripts").mkdir()
        (skill / "SKILL.md").write_text("# seed skill", encoding="utf-8")
        (skill / "references" / "tpl.md").write_text("seed template", encoding="utf-8")
        (skill / "scripts" / "run.py").write_text("print()", encoding="utf-8")
        return skill

    def _adapter(self, tmp_path):
        a = SkillEvalAdapter(
            split_dir=_make_split_dir(tmp_path), split_mode="split_dir",
            skill_dir=str(self._skill_dir(tmp_path)),
            trainable_files=["references/tpl.md"],
        )
        a.setup({})
        return a

    def test_trainable_excluded_from_frozen_support(self, tmp_path) -> None:
        a = self._adapter(tmp_path)
        rels = [rel for _, rel in (a.skill_files or [])]
        assert os.path.join("scripts", "run.py") in rels
        assert "references/tpl.md" not in [r.replace(os.sep, "/") for r in rels]

    def test_rollout_splits_bundle_into_skill_md_and_docs(self, tmp_path, monkeypatch) -> None:
        a = self._adapter(tmp_path)
        items = a.build_train_env(batch_size=2, seed=1)
        captured = {}

        def fake_run_batch(batch_items, skill_content, out_dir, **kw):
            captured["skill_md"] = skill_content
            captured.update(kw)
            return [{"id": it["id"], "task_type": "default", "response": "r",
                     "duration_s": 0.1, "work_dir": "/nx"} for it in batch_items]

        monkeypatch.setattr(adapter_mod, "run_batch", fake_run_batch)
        monkeypatch.setattr(adapter_mod, "judge",
                            lambda item, response, listing: {"id": item["id"], "hard": 1, "soft": 1.0})
        state = build_bundle("# evolved skill", [("references/tpl.md", "evolved template")])
        a.rollout(items, state, str(tmp_path / "out"))
        assert captured["skill_md"] == "# evolved skill"
        assert captured["skill_docs"] == {"references/tpl.md": "evolved template"}

    def test_mangled_section_falls_back_to_seed(self, tmp_path) -> None:
        a = self._adapter(tmp_path)
        # optimizer destroyed the template header: section vanishes from parse
        state = "<!-- FILE: SKILL.md -->\n# evolved skill"
        skill_md, docs = a._split_state(state)
        assert skill_md == "# evolved skill"
        assert docs == {"references/tpl.md": "seed template"}

    def test_without_trainable_files_state_is_skill_md(self, tmp_path) -> None:
        a = SkillEvalAdapter(split_dir=_make_split_dir(tmp_path), split_mode="split_dir")
        assert a._split_state("# plain") == ("# plain", None)

    def test_validation_fails_fast(self, tmp_path) -> None:
        split_dir = _make_split_dir(tmp_path)
        skill = self._skill_dir(tmp_path)
        with pytest.raises(ValueError, match="skill_dir"):
            SkillEvalAdapter(split_dir=split_dir, split_mode="split_dir",
                             trainable_files=["a.md"])
        with pytest.raises(ValueError, match="not found"):
            SkillEvalAdapter(split_dir=split_dir, split_mode="split_dir",
                             skill_dir=str(skill), trainable_files=["references/nx.md"])
        with pytest.raises(ValueError, match="SKILL.md"):
            SkillEvalAdapter(split_dir=split_dir, split_mode="split_dir",
                             skill_dir=str(skill), trainable_files=["SKILL.md"])

    def test_trainable_files_accepts_comma_string(self, tmp_path) -> None:
        skill = self._skill_dir(tmp_path)
        a = SkillEvalAdapter(split_dir=_make_split_dir(tmp_path), split_mode="split_dir",
                             skill_dir=str(skill), trainable_files="references/tpl.md")
        assert a.trainable_files == ["references/tpl.md"]
