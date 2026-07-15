"""Tests for the skilleval environment (custom skill evaluation)."""
from __future__ import annotations

import errno
import json
import os
import stat
from types import SimpleNamespace

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

    @pytest.mark.parametrize(
        "bad_path",
        [
            "../secret.txt",
            "data/../secret.txt",
            "/tmp/secret.txt",
            r"data\secret.txt",
            "task.md",
            "task.md/notes.txt",
            ".agents",
            ".agents/skills/target/SKILL.md",
        ],
    )
    def test_unsafe_or_runtime_colliding_file_path_raises(self, tmp_path, bad_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item(files={bad_path: "no"})])
        with pytest.raises(ValueError, match="'files' path"):
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
        assert task["judge_mode"] == "auto"
        assert task["_judge_mode_explicit"] is False
        assert task["artifact_checks"] == []

    def test_normalization_preserves_values(self, tmp_path) -> None:
        path = _write_tasks(
            tmp_path,
            [_valid_item(task_type="qa", files={"a.txt": "hello"})],
        )
        task = load_tasks(path)[0]
        assert task["task_type"] == "qa"
        assert task["files"] == {"a.txt": "hello"}

    def test_non_string_task_type_raises(self, tmp_path) -> None:
        path = _write_tasks(tmp_path, [_valid_item(task_type={"bad": True})])
        with pytest.raises(ValueError, match="'task_type' must be a string"):
            load_tasks(path)

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

from skillopt.envs.skilleval import artifacts as artifacts_mod  # noqa: E402
from skillopt.envs.skilleval import rollout as rollout_mod  # noqa: E402
from skillopt.envs.skilleval.rollout import GUIDE_PROMPT, run_batch  # noqa: E402


def _three_items():
    return [
        _valid_item("t1"),
        _valid_item("t2"),
        _valid_item("t3", task_type="qa"),
    ]


class TestRunBatch:
    @staticmethod
    def _seed_workspace(**kwargs):
        work_dir = kwargs["work_dir"]
        os.makedirs(work_dir, exist_ok=True)
        with open(os.path.join(work_dir, "task.md"), "w", encoding="utf-8") as handle:
            handle.write(kwargs.get("task_text", ""))
        for rel_path, content in (kwargs.get("extra_files") or {}).items():
            path = os.path.join(work_dir, rel_path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        return "", ""

    def _patch_harness(self, monkeypatch, exec_fn):
        prepared = []

        def fake_prepare(**kwargs):
            prepared.append(kwargs)
            return self._seed_workspace(**kwargs)

        monkeypatch.setattr(rollout_mod, "prepare_workspace", fake_prepare)
        monkeypatch.setattr(rollout_mod, "run_claude_code_exec", exec_fn)
        return prepared

    @staticmethod
    def _deny_mode_zero_opens(monkeypatch, blocked_name):
        real_open = artifacts_mod.os.open

        def deny_mode_zero(path, flags, *args, **kwargs):
            dir_fd = kwargs.get("dir_fd")
            if dir_fd is not None and os.fspath(path) == blocked_name:
                info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_IMODE(info.st_mode) == 0:
                    raise PermissionError(
                        errno.EACCES,
                        "injected permission denial",
                        os.fspath(path),
                    )
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(artifacts_mod.os, "open", deny_mode_zero)

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

        monkeypatch.setattr(rollout_mod, "prepare_workspace", self._seed_workspace)
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
        monkeypatch.setattr(rollout_mod, "prepare_workspace", self._seed_workspace)
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

    def test_plugin_runtime_installs_every_skill_without_leaking_targets(
        self, tmp_path, monkeypatch
    ) -> None:
        prepared = []
        exec_calls = []
        monkeypatch.setattr(
            rollout_mod,
            "prepare_workspace",
            lambda **kwargs: prepared.append(kwargs) or self._seed_workspace(**kwargs),
        )
        monkeypatch.setattr(
            rollout_mod,
            "run_claude_code_exec",
            lambda **kwargs: exec_calls.append(kwargs) or ("ok", "raw"),
        )
        skills = [
            {
                "name": "alpha",
                "content": "# Alpha",
                "files": [("/source/alpha/run.py", "scripts/run.py")],
            },
            {
                "name": "beta",
                "content": "# Beta",
                "files": [("/source/beta/doc.md", "references/doc.md")],
            },
        ]
        item = _valid_item(
            "t1",
            target_skills=["beta"],
            task_type="routing",
        )
        result = run_batch(
            [item],
            "# Alpha",
            str(tmp_path),
            runtime_skills=skills,
        )[0]

        assert prepared[0]["installed_skills"] == [
            ("alpha", "# Alpha"),
            ("beta", "# Beta"),
        ]
        assert prepared[0]["copy_files"] == [
            (
                "/source/alpha/run.py",
                os.path.join(".agents", "skills", "alpha", "scripts", "run.py"),
            ),
            (
                "/source/beta/doc.md",
                os.path.join(".agents", "skills", "beta", "references", "doc.md"),
            ),
        ]
        assert prepared[0]["task_text"] == item["question"]
        assert "beta" not in prepared[0]["task_text"]
        assert exec_calls[0]["prompt"] == rollout_mod.PLUGIN_GUIDE_PROMPT
        assert result["target_skills"] == ["beta"]

    def test_captures_created_and_modified_outputs(self, tmp_path, monkeypatch) -> None:
        def produce(*, work_dir, **kwargs):
            with open(os.path.join(work_dir, "input.txt"), "w", encoding="utf-8") as handle:
                handle.write("changed")
            with open(os.path.join(work_dir, "report.pdf"), "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            os.makedirs(os.path.join(work_dir, ".claude"), exist_ok=True)
            with open(
                os.path.join(work_dir, ".claude", "runtime.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("{}")
            return "done", "raw"

        self._patch_harness(monkeypatch, produce)
        result = run_batch(
            [_valid_item("t1", files={"input.txt": "seed", "unchanged.txt": "same"})],
            "# skill",
            str(tmp_path),
        )[0]

        assert result["response"] == "done"
        assert [(row["path"], row["change"]) for row in result["artifacts"]] == [
            ("input.txt", "modified"),
            ("report.pdf", "created"),
        ]
        assert result["artifacts"][1]["kind"] == "pdf"

    def test_captures_outputs_when_target_fails(self, tmp_path, monkeypatch) -> None:
        def produce_then_fail(*, work_dir, **kwargs):
            with open(os.path.join(work_dir, "partial.pdf"), "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            raise RuntimeError("CLI crashed")

        self._patch_harness(monkeypatch, produce_then_fail)
        result = run_batch([_valid_item("t1")], "# skill", str(tmp_path))[0]

        assert "RuntimeError: CLI crashed" in result["error"]
        assert [(row["path"], row["change"]) for row in result["artifacts"]] == [
            ("partial.pdf", "created")
        ]

    def test_forbidden_target_entry_is_artifact_failure_and_batch_isolated(
        self, tmp_path, monkeypatch
    ) -> None:
        def produce(*, work_dir, **kwargs):
            if work_dir.endswith("t1"):
                os.symlink("/tmp", os.path.join(work_dir, "forbidden"))
            else:
                with open(os.path.join(work_dir, "ok.pdf"), "wb") as handle:
                    handle.write(b"%PDF-1.4\n")
            return "done", "raw"

        self._patch_harness(monkeypatch, produce)
        results = run_batch(_three_items()[:2], "# skill", str(tmp_path), workers=2)

        assert results[0]["response"] == "done"
        assert "symlink directory" in results[0]["artifact_error"]
        assert results[0]["artifact_failure"] is True
        assert results[0]["artifact_error_type"] == "target_validation"
        assert results[0]["score_valid"] is True
        assert results[0]["error"] == results[0]["artifact_error"]
        assert results[0]["artifacts"] == []
        assert [row["path"] for row in results[1]["artifacts"]] == ["ok.pdf"]
        assert "artifact_error" not in results[1]

        judged = []

        def fake_judge(item, response, listing):
            judged.append(item["id"])
            return {"hard": 1, "soft": 1.0, "judge_reason": "ok"}

        scored = evaluator.merge_scores(_three_items()[:2], results, fake_judge)
        assert judged == ["t2"]
        assert scored[0]["hard"] == 0
        assert scored[0]["soft"] == 0.0

    def test_hard_linked_target_output_is_artifact_failure_and_batch_isolated(
        self, tmp_path, monkeypatch
    ) -> None:
        def produce(*, work_dir, **kwargs):
            output = os.path.join(work_dir, "report.pdf")
            with open(output, "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            if work_dir.endswith("t1"):
                os.link(output, os.path.join(work_dir, "report-copy.pdf"))
            return "done", "raw"

        self._patch_harness(monkeypatch, produce)
        results = run_batch(_three_items()[:2], "# skill", str(tmp_path), workers=2)

        assert results[0]["response"] == "done"
        assert "single-link" in results[0]["artifact_error"]
        assert results[0]["artifact_failure"] is True
        assert results[0]["artifacts"] == []
        assert results[0]["artifact_error_type"] == "target_validation"
        assert results[0]["score_valid"] is True
        assert results[0]["error"] == results[0]["artifact_error"]
        assert [row["path"] for row in results[1]["artifacts"]] == ["report.pdf"]
        assert "artifact_error" not in results[1]

    def test_codex_last_message_is_not_target_output(
        self, tmp_path, monkeypatch
    ) -> None:
        def produce(*, work_dir, **kwargs):
            with open(
                os.path.join(work_dir, "codex_last_message.txt"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("final response")
            with open(os.path.join(work_dir, "report.pdf"), "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            return "done", "raw"

        monkeypatch.setattr(rollout_mod, "prepare_workspace", self._seed_workspace)
        monkeypatch.setattr(rollout_mod, "run_codex_exec", produce)
        monkeypatch.setattr(rollout_mod, "get_target_backend", lambda: "codex_exec")

        result = run_batch([_valid_item("t1")], "# skill", str(tmp_path))[0]

        assert result["response"] == "done"
        assert [row["path"] for row in result["artifacts"]] == ["report.pdf"]
        assert "artifact_error" not in result

    @pytest.mark.parametrize("entry_type", ["file", "directory"])
    def test_target_permission_denial_is_scoreable_artifact_failure(
        self, tmp_path, monkeypatch, entry_type
    ) -> None:
        blocked_name = "blocked.pdf" if entry_type == "file" else "blocked"
        self._deny_mode_zero_opens(monkeypatch, blocked_name)

        def produce(*, work_dir, **kwargs):
            blocked = os.path.join(work_dir, blocked_name)
            if entry_type == "file":
                with open(blocked, "wb") as handle:
                    handle.write(b"%PDF-1.4\n")
            else:
                os.mkdir(blocked)
            os.chmod(blocked, 0)
            return "done", "raw"

        self._patch_harness(monkeypatch, produce)
        result = run_batch([_valid_item("t1")], "# skill", str(tmp_path))[0]
        blocked = os.path.join(result["work_dir"], blocked_name)
        os.chmod(blocked, 0o700 if entry_type == "directory" else 0o600)

        assert result["response"] == "done"
        assert "permission denial" in result["artifact_error"]
        assert result["artifact_failure"] is True
        assert result["artifact_error_type"] == "target_validation"
        assert result["score_valid"] is True
        assert result["error"] == result["artifact_error"]
        assert "artifact_collection_error" not in result

    def test_baseline_permission_denial_remains_infrastructure_invalid(
        self, tmp_path, monkeypatch
    ) -> None:
        blocked_name = "blocked.pdf"
        target_called = False
        self._deny_mode_zero_opens(monkeypatch, blocked_name)

        def prepare(**kwargs):
            self._seed_workspace(**kwargs)
            blocked = os.path.join(kwargs["work_dir"], blocked_name)
            with open(blocked, "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            os.chmod(blocked, 0)

        def target(**kwargs):
            nonlocal target_called
            target_called = True
            return "done", "raw"

        monkeypatch.setattr(rollout_mod, "prepare_workspace", prepare)
        monkeypatch.setattr(rollout_mod, "run_claude_code_exec", target)
        result = run_batch([_valid_item("t1")], "# skill", str(tmp_path))[0]
        os.chmod(os.path.join(result["work_dir"], blocked_name), 0o600)

        assert target_called is False
        assert "permission denial" in result["artifact_collection_error"]
        assert result["artifact_collection_error_type"] == "infrastructure"
        assert result["score_valid"] is False
        assert "artifact_error" not in result
        assert "error" not in result

    def test_artifact_collection_failure_is_invalid_not_target_zero(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        error_type = getattr(
            rollout_mod,
            "ArtifactCollectionError",
            RuntimeError,
        )
        manifest_calls = 0

        def fail_post_manifest(work_dir):
            nonlocal manifest_calls
            manifest_calls += 1
            if manifest_calls == 1:
                return {}
            raise error_type("injected collection failure")

        self._patch_harness(monkeypatch, lambda **kwargs: ("done", "raw"))
        monkeypatch.setattr(rollout_mod, "build_manifest", fail_post_manifest)

        item = _valid_item("t1")
        result = run_batch([item], "# skill", str(tmp_path))[0]

        assert result["response"] == "done"
        assert "injected collection failure" in result["artifact_collection_error"]
        assert result["artifact_collection_error_type"] == "infrastructure"
        assert result["score_valid"] is False
        assert "artifact_error" not in result
        assert "error" not in result

        judged = []

        def fake_judge(*args):
            judged.append(args)
            return {"hard": 1, "soft": 1.0, "judge_reason": "wrong"}

        scored = evaluator.merge_scores([item], [result], fake_judge)[0]
        assert judged == []
        assert scored["score_valid"] is False
        assert scored["judge_skipped"] == "invalid_rollout"
        assert "rollout finished: 0 ok, 0 errored, 1 invalid" in capsys.readouterr().out

    def test_target_execution_error_is_preserved_with_artifact_validation_error(
        self, tmp_path, monkeypatch
    ) -> None:
        def produce_then_fail(*, work_dir, **kwargs):
            os.symlink("/tmp", os.path.join(work_dir, "forbidden"))
            raise RuntimeError("CLI crashed")

        self._patch_harness(monkeypatch, produce_then_fail)

        result = run_batch([_valid_item("t1")], "# skill", str(tmp_path))[0]

        assert "RuntimeError: CLI crashed" in result["error"]
        assert result["artifact_error_type"] == "target_validation"
        assert "symlink" in result["artifact_error"]
        assert result["score_valid"] is True


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


class TestBuildReportInvalidAware:
    """Invalid (score_valid=False) rows are excluded from pass/soft denominators
    and listed separately; agentic rows show criterion evidence and coverage."""

    def test_invalid_evaluations_excluded_from_pass_rate(self) -> None:
        report = evaluate_skill.build_report([
            {"id": "valid", "hard": 1, "soft": 1.0, "score_valid": True,
             "judge_status": "valid_pass", "judge_reason": "ok", "duration_s": 1},
            {"id": "infra", "hard": 0, "soft": 0.0, "score_valid": False,
             "judge_status": "evaluation_error", "judge_error": "timeout", "duration_s": 2},
        ])
        assert "Scored tasks: 1" in report
        assert "Invalid evaluations: 1" in report
        assert "100.0%" in report

    def test_invalid_row_listed_separately_from_scored_failures(self) -> None:
        report = evaluate_skill.build_report([
            {"id": "infra", "hard": 0, "soft": 0.0, "score_valid": False,
             "judge_status": "evaluation_error", "judge_error": "sandbox probe failed",
             "duration_s": 1},
        ])
        assert "## Invalid evaluations" in report
        assert "`infra`" in report
        assert "sandbox probe failed" in report

    def test_agentic_criteria_and_coverage_are_rendered(self) -> None:
        report = evaluate_skill.build_report([
            {"id": "t1", "hard": 1, "soft": 1.0, "score_valid": True,
             "judge_mode": "agentic", "judge_status": "valid_pass", "judge_reason": "ok",
             "duration_s": 1,
             "judge_criteria": [
                 {"id": "visual", "passed": True, "score": 1.0, "reason": "clear",
                  "evidence": [{"path": "report.pdf", "locator": "page=1", "source": "render"}]},
             ],
             "judge_coverage": {"artifacts": ["report.pdf"],
                                "units_inspected": ["report.pdf:page=1"],
                                "units_omitted": []}},
        ])
        assert "visual" in report
        assert "report.pdf" in report
        assert "page=1" in report

    def test_legacy_text_report_fields_and_headings_retained(self) -> None:
        # No score_valid / judge_status keys: behaves as the pre-agentic report.
        report = evaluate_skill.build_report([
            _result("t1", 1, 0.9, judge_reason="meets rubric"),
            _result("t2", 0, 0.2, judge_reason="missing totals"),
        ])
        assert "# Skill Evaluation Report" in report
        assert "- Tasks: 2" in report
        assert "- Pass rate (hard): 50.0%" in report
        assert "## By task type" in report
        assert "## Tasks" in report
        assert "## Cost" in report
        assert "## Failures\n\nnone" in report


_JUDGE_CLI_DEFAULTS = dict(
    judge_mode="auto",
    judge_exec_backend="claude_code_exec",
    judge_exec_model="",
    judge_exec_timeout=300,
    judge_exec_effort="low",
    judge_cache=True,
    judge_sandbox_command="bwrap",
    judge_max_evidence_bytes=536_870_912,
    judge_max_scratch_bytes=1_073_741_824,
    judge_max_render_pixels=500_000_000,
)


class TestJudgeExecCli:
    """The standalone CLI's judge-exec flags must reach evaluate_rollouts as a
    fully-populated, independent AgenticJudgeConfig."""

    def test_judge_exec_flags_reach_evaluate_rollouts(self, tmp_path, monkeypatch) -> None:
        skill = tmp_path / "alpha"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: alpha\n---\n# alpha\n", encoding="utf-8")
        tasks = _write_tasks(tmp_path, [_valid_item()])
        out_root = tmp_path / "out"
        argv = [
            "evaluate_skill.py",
            "--skill", str(skill),
            "--tasks", tasks,
            "--out_root", str(out_root),
            "--judge_mode", "auto",
            "--judge_exec_backend", "codex_exec",
            "--judge_exec_model", "gpt-5.5-codex",
            "--judge_exec_timeout", "240",
            "--judge_exec_effort", "low",
            "--judge_sandbox_command", "sudo -n bwrap",
            "--no-judge_cache",
            "--workers", "1",
        ]
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(evaluate_skill, "_configure_backends", lambda _args: None)
        monkeypatch.setattr(
            evaluate_skill, "run_batch",
            lambda items, *a, **k: [{"id": items[0]["id"], "response": "ok",
                                     "duration_s": 0.1, "work_dir": "/nx", "artifacts": []}],
        )
        captured = {}

        def fake_eval(items, rollouts, *, state_hash, out_root, judge_config, chat_judge=None):
            captured["judge_config"] = judge_config
            captured["state_hash"] = state_hash
            return [_result(items[0]["id"], 1, 1.0, score_valid=True, judge_status="valid_pass")]

        monkeypatch.setattr(evaluate_skill, "evaluate_rollouts", fake_eval)

        evaluate_skill.main()

        cfg = captured["judge_config"]
        assert cfg.mode == "auto"
        assert cfg.backend == "codex_exec"
        assert cfg.model == "gpt-5.5-codex"
        assert cfg.timeout == 240
        assert cfg.effort == "low"
        assert cfg.cache is False
        assert cfg.sandbox_command == ("sudo", "-n", "bwrap")
        assert isinstance(captured["state_hash"], str) and captured["state_hash"]

    def test_empty_sandbox_command_is_rejected(self, tmp_path, monkeypatch) -> None:
        skill = tmp_path / "alpha"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: alpha\n---\n# alpha\n", encoding="utf-8")
        tasks = _write_tasks(tmp_path, [_valid_item()])
        argv = [
            "evaluate_skill.py",
            "--skill", str(skill),
            "--tasks", tasks,
            "--out_root", str(tmp_path / "out"),
            "--judge_sandbox_command", "   ",
        ]
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(evaluate_skill, "_configure_backends", lambda _args: None)
        with pytest.raises(SystemExit, match="sandbox"):
            evaluate_skill.main()


class TestSkillEvalConfigJudgeDefaults:
    """The YAML judge_* defaults must flatten onto adapter kwargs and reach the
    AgenticJudgeConfig unchanged (no skillopt/config.py mapping needed)."""

    def test_yaml_defaults_map_to_judge_config(self) -> None:
        import inspect as _inspect

        from skillopt.config import flatten_config, load_config

        cfg_path = os.path.join(
            os.path.dirname(__file__), "..", "configs", "skilleval", "default.yaml"
        )
        flat = flatten_config(load_config(cfg_path))
        assert flat["judge_mode"] == "auto"
        assert flat["judge_backend"] == "claude_code_exec"
        assert flat["judge_model"] == ""
        assert flat["judge_timeout"] == 300
        assert flat["judge_effort"] == "low"
        assert flat["judge_cache"] is True
        assert flat["judge_max_evidence_bytes"] == 536_870_912
        assert flat["judge_max_scratch_bytes"] == 1_073_741_824
        assert flat["judge_max_render_pixels"] == 500_000_000

        accepted = set(_inspect.signature(SkillEvalAdapter.__init__).parameters) - {"self"}
        adapter = SkillEvalAdapter(**{k: v for k, v in flat.items() if k in accepted})
        assert adapter.judge_config.mode == "auto"
        assert adapter.judge_config.backend == "claude_code_exec"
        assert adapter.judge_config.timeout == 300
        assert adapter.judge_config.effort == "low"
        assert adapter.judge_config.sandbox_command == ("bwrap",)
        assert adapter.judge_config.max_evidence_bytes == 536_870_912


class TestPluginAggregation:
    def test_attributes_multi_target_and_weakest_skill(self) -> None:
        results = [
            _result("t1", 1, 1.0, task_type="routing", target_skills=["alpha"]),
            _result("t2", 0, 0.4, task_type="integration", target_skills=["alpha", "beta"]),
        ]
        summary = evaluate_skill.aggregate_results(results, ["alpha", "beta"])
        assert summary["overall"] == {
            "count": 2, "hard": 0.5, "soft": 0.7, "scored_count": 2, "invalid_count": 0,
        }
        assert summary["by_skill"]["alpha"]["count"] == 2
        assert summary["by_skill"]["beta"] == {
            "count": 1, "hard": 0.0, "soft": 0.4, "scored_count": 1, "invalid_count": 0,
        }
        assert summary["routing"]["count"] == 1
        assert summary["integration"]["count"] == 1
        assert summary["weakest_skill"]["name"] == "beta"
        assert summary["mode"] == "plugin"
        assert summary["skill_names"] == ["alpha", "beta"]

    def test_metrics_report_scored_and_invalid_counts_and_exclude_invalid_from_mean(self) -> None:
        results = [
            _result("t1", 1, 1.0, target_skills=["alpha"]),
            _result("t2", 0, 0.0, target_skills=["alpha"], score_valid=False),
        ]
        summary = evaluate_skill.aggregate_results(results, ["alpha"])
        overall = summary["overall"]
        assert overall["count"] == 2
        assert overall["scored_count"] == 1
        assert overall["invalid_count"] == 1
        # the invalid row must not silently pull down the aggregate hard/soft mean
        assert overall["hard"] == 1.0
        assert overall["soft"] == 1.0

    def test_require_valid_raises_on_any_invalid_row(self) -> None:
        results = [
            _result("t1", 1, 1.0, target_skills=["alpha"]),
            _result("t2", 0, 0.0, target_skills=["alpha"], score_valid=False),
        ]
        with pytest.raises(ValueError, match="score_valid"):
            evaluate_skill.aggregate_results(results, ["alpha"], require_valid=True)

    def test_require_valid_defaults_false_and_does_not_raise(self) -> None:
        results = [_result("t1", 0, 0.0, target_skills=["alpha"], score_valid=False)]
        summary = evaluate_skill.aggregate_results(results, ["alpha"])
        assert summary["overall"]["invalid_count"] == 1

    def test_normalize_rejects_unknown_target(self) -> None:
        items = [_valid_item(target_skills=["unknown"])]
        with pytest.raises(ValueError, match="unknown skills"):
            evaluate_skill.normalize_plugin_tasks(items, {"alpha"})

    @pytest.mark.parametrize("bad_targets", [[], "alpha", [""], [1]])
    def test_normalize_rejects_malformed_targets(self, bad_targets) -> None:
        with pytest.raises(ValueError, match="target_skills"):
            evaluate_skill.normalize_plugin_tasks(
                [_valid_item(target_skills=bad_targets)],
                {"alpha"},
            )

    def test_normalize_defaults_absent_targets_and_strips_values(self) -> None:
        items = [
            _valid_item(task_type="  routing  ", target_skills=[" alpha ", "alpha"]),
            _valid_item("t2", task_type="   "),
        ]
        evaluate_skill.normalize_plugin_tasks(items, {"alpha"})
        assert items[0]["target_skills"] == ["alpha"]
        assert items[0]["task_type"] == "routing"
        assert items[1]["target_skills"] == []
        assert items[1]["task_type"] == "default"

    def test_normalize_rejects_non_string_task_type(self) -> None:
        with pytest.raises(ValueError, match="task_type must be a string"):
            evaluate_skill.normalize_plugin_tasks(
                [_valid_item(task_type={"bad": True})],
                {"alpha"},
            )

    def test_unassigned_tasks_only_contribute_to_overall_and_type(self) -> None:
        summary = evaluate_skill.aggregate_results(
            [_result("t1", 1, 0.8, task_type="general", target_skills=[])],
            ["alpha"],
        )
        assert summary["overall"]["count"] == 1
        assert summary["by_task_type"]["general"]["count"] == 1
        assert summary["by_skill"]["alpha"]["count"] == 0
        assert summary["weakest_skill"] is None


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


class TestShouldUseAgentic:
    """Unit coverage of the mode-resolution formula, independent of I/O."""

    def test_explicit_chat_wins_over_binary_artifact(self) -> None:
        item = {"judge_mode": "chat", "_judge_mode_explicit": True, "artifact_checks": []}
        result = {"artifacts": [{"path": "a.xlsx", "mime": "", "change": "created"}]}
        assert evaluator.should_use_agentic(item, result, evaluator.AgenticJudgeConfig()) is False

    def test_explicit_agentic_wins_over_text_only(self) -> None:
        item = {"judge_mode": "agentic", "_judge_mode_explicit": True, "artifact_checks": []}
        result = {"artifacts": [{"path": "a.txt", "mime": "text/plain", "change": "created"}]}
        assert evaluator.should_use_agentic(item, result, None) is True

    def test_non_explicit_task_field_is_ignored_in_favor_of_environment_default(self) -> None:
        # The task's own judge_mode="chat" was never explicitly set by the task
        # author (no _judge_mode_explicit) so the environment default (agentic)
        # governs instead.
        item = {"judge_mode": "chat", "artifact_checks": []}
        result = {"artifacts": [{"path": "a.xlsx", "mime": "", "change": "created"}]}
        config = evaluator.AgenticJudgeConfig(mode="agentic")
        assert evaluator.should_use_agentic(item, result, config) is True

    def test_auto_routes_on_supported_detected_kind(self) -> None:
        item = {"judge_mode": "auto", "artifact_checks": []}
        result = {"artifacts": [{"path": "report.xlsx", "mime": "application/zip", "change": "created"}]}
        assert evaluator.should_use_agentic(item, result, evaluator.AgenticJudgeConfig()) is True

    def test_auto_routes_on_structured_check_naming_supported_binary_path(self) -> None:
        item = {"judge_mode": "auto", "artifact_checks": [{"path": "report.docx"}]}
        result = {"artifacts": []}
        assert evaluator.should_use_agentic(item, result, evaluator.AgenticJudgeConfig()) is True

    def test_unknown_binary_format_does_not_route(self) -> None:
        item = {"judge_mode": "auto", "artifact_checks": []}
        result = {"artifacts": [{"path": "archive.zip", "mime": "application/zip", "change": "created"}]}
        assert evaluator.should_use_agentic(item, result, evaluator.AgenticJudgeConfig()) is False

    def test_deleted_artifact_does_not_route(self) -> None:
        item = {"judge_mode": "auto", "artifact_checks": []}
        result = {"artifacts": [{"path": "report.xlsx", "mime": "", "change": "deleted"}]}
        assert evaluator.should_use_agentic(item, result, evaluator.AgenticJudgeConfig()) is False

    def test_auto_with_no_judge_config_defaults_to_auto_routing(self) -> None:
        item = {"judge_mode": "auto", "artifact_checks": []}
        result = {"artifacts": [{"path": "report.pdf", "mime": "", "change": "created"}]}
        assert evaluator.should_use_agentic(item, result, None) is True


class TestEvaluateRollouts:
    def test_auto_mode_routes_binary_artifact_to_agentic_judge(self, tmp_path, monkeypatch) -> None:
        called = []
        monkeypatch.setattr(
            evaluator,
            "run_agentic_judge",
            lambda **kwargs: called.append(kwargs) or {
                "id": "t1", "hard": 1, "soft": 1.0, "judge_reason": "ok",
                "judge_mode": "agentic", "judge_status": "valid_pass",
                "score_valid": True,
            },
        )
        results = evaluator.evaluate_rollouts(
            [{"id": "t1", "question": "q", "rubric": "r",
              "judge_mode": "auto", "artifact_checks": []}],
            [{"id": "t1", "response": "done", "work_dir": str(tmp_path),
              "artifacts": [{"path": "report.xlsx", "mime": "application/zip",
                             "change": "created"}]}],
            state_hash="state",
            out_root=str(tmp_path / "out"),
            judge_config=evaluator.AgenticJudgeConfig(),
        )
        assert results[0]["judge_mode"] == "agentic"
        assert len(called) == 1

    def test_auto_mode_keeps_text_on_chat_judge(self, tmp_path) -> None:
        results = evaluator.evaluate_rollouts(
            [{"id": "t1", "question": "q", "rubric": "r",
              "judge_mode": "auto", "artifact_checks": []}],
            [{"id": "t1", "response": "done", "work_dir": str(tmp_path),
              "artifacts": [{"path": "answer.txt", "mime": "text/plain",
                             "change": "created"}]}],
            state_hash="state",
            out_root=str(tmp_path / "out"),
            judge_config=None,
            chat_judge=lambda item, response, listing: {
                "id": item["id"], "hard": 1, "soft": 1.0,
                "judge_reason": "ok", "score_valid": True,
            },
        )
        assert results[0]["hard"] == 1

    def test_rollout_error_short_circuits_judging(self, tmp_path, monkeypatch) -> None:
        def _fail_if_called(**kwargs):
            raise AssertionError("agentic judge must not run for an errored rollout")

        def _chat_fail_if_called(item, response, listing):
            raise AssertionError("chat judge must not run for an errored rollout")

        monkeypatch.setattr(evaluator, "run_agentic_judge", _fail_if_called)
        results = evaluator.evaluate_rollouts(
            [{"id": "t1", "question": "q", "rubric": "r",
              "judge_mode": "auto", "artifact_checks": []}],
            [{"id": "t1", "response": "", "error": "boom", "work_dir": str(tmp_path), "artifacts": []}],
            state_hash="state",
            out_root=str(tmp_path / "out"),
            judge_config=evaluator.AgenticJudgeConfig(),
            chat_judge=_chat_fail_if_called,
        )
        assert results[0]["hard"] == 0
        assert results[0]["soft"] == 0.0
        assert results[0]["judge_status"] == "artifact_failure"
        assert results[0]["score_valid"] is True
        assert results[0]["error"] == "boom"

    def test_score_valid_false_without_error_key_skips_agentic_judge(self, tmp_path, monkeypatch) -> None:
        # rollout.py's artifact-collection-error path sets score_valid=False
        # without ever setting "error" -- the row must never reach the
        # agentic judge (which would stamp score_valid=True and silently
        # reclassify an infrastructure failure as a scored result).
        calls = []
        monkeypatch.setattr(
            evaluator,
            "run_agentic_judge",
            lambda **kwargs: calls.append(kwargs) or {
                "id": "t1", "hard": 1, "soft": 1.0, "judge_reason": "ok",
                "judge_mode": "agentic", "judge_status": "valid_pass", "score_valid": True,
            },
        )
        results = evaluator.evaluate_rollouts(
            [{"id": "t1", "question": "q", "rubric": "r", "judge_mode": "auto",
              "artifact_checks": [{"path": "report.xlsx"}]}],
            [{"id": "t1", "response": "done", "work_dir": str(tmp_path), "artifacts": [],
              "score_valid": False, "artifact_collection_error": "PermissionError: denied"}],
            state_hash="state",
            out_root=str(tmp_path / "out"),
            judge_config=evaluator.AgenticJudgeConfig(),
        )
        assert calls == []
        assert results[0]["score_valid"] is False
        assert results[0]["hard"] == 0
        assert results[0]["soft"] == 0.0
        assert results[0]["judge_status"] == "evaluation_error"

    def test_score_valid_false_without_error_key_skips_chat_judge(self, tmp_path) -> None:
        calls = []

        def _record_chat_judge(item, response, listing):
            calls.append(item)
            return {"id": item["id"], "hard": 1, "soft": 1.0, "judge_reason": "ok", "score_valid": True}

        results = evaluator.evaluate_rollouts(
            [{"id": "t1", "question": "q", "rubric": "r", "judge_mode": "chat",
              "_judge_mode_explicit": True, "artifact_checks": []}],
            [{"id": "t1", "response": "done", "work_dir": str(tmp_path), "artifacts": [],
              "score_valid": False, "artifact_collection_error": "RuntimeError: boom"}],
            state_hash="state",
            out_root=str(tmp_path / "out"),
            judge_config=None,
            chat_judge=_record_chat_judge,
        )
        assert calls == []
        assert results[0]["score_valid"] is False
        assert results[0]["hard"] == 0
        assert results[0]["soft"] == 0.0
        assert results[0]["judge_status"] == "evaluation_error"

    def test_missing_judge_config_returns_invalid_result_when_agentic_needed(self, tmp_path) -> None:
        results = evaluator.evaluate_rollouts(
            [{"id": "t1", "question": "q", "rubric": "r", "judge_mode": "agentic",
              "_judge_mode_explicit": True, "artifact_checks": []}],
            [{"id": "t1", "response": "done", "work_dir": str(tmp_path), "artifacts": []}],
            state_hash="state",
            out_root=str(tmp_path / "out"),
            judge_config=None,
        )
        assert results[0]["score_valid"] is False
        assert results[0]["judge_status"] == "evaluation_error"
        assert "not configured" in results[0]["judge_error"]

    def test_chat_judge_exception_returns_evaluation_error_not_legacy_zero(self, tmp_path) -> None:
        def _boom(item, response, listing):
            raise RuntimeError("optimizer unavailable")

        results = evaluator.evaluate_rollouts(
            [{"id": "t1", "question": "q", "rubric": "r", "judge_mode": "chat",
              "_judge_mode_explicit": True, "artifact_checks": []}],
            [{"id": "t1", "response": "done", "work_dir": str(tmp_path), "artifacts": []}],
            state_hash="state",
            out_root=str(tmp_path / "out"),
            judge_config=None,
            chat_judge=_boom,
        )
        assert results[0]["hard"] == 0
        assert results[0]["score_valid"] is False
        assert results[0]["judge_status"] == "evaluation_error"
        assert "judge_error" in results[0]

    def test_chat_judge_parse_failure_returns_evaluation_error(self, tmp_path) -> None:
        def _unparseable(item, response, listing):
            return {"id": item["id"], "hard": 0, "soft": 0.0, "judge_reason": "",
                     "judge_error": "unparseable judge reply"}

        results = evaluator.evaluate_rollouts(
            [{"id": "t1", "question": "q", "rubric": "r", "judge_mode": "chat",
              "_judge_mode_explicit": True, "artifact_checks": []}],
            [{"id": "t1", "response": "done", "work_dir": str(tmp_path), "artifacts": []}],
            state_hash="state",
            out_root=str(tmp_path / "out"),
            judge_config=None,
            chat_judge=_unparseable,
        )
        assert results[0]["score_valid"] is False
        assert results[0]["judge_status"] == "evaluation_error"

    def test_length_mismatch_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            evaluator.evaluate_rollouts(
                [{"id": "t1", "question": "q", "rubric": "r",
                  "judge_mode": "auto", "artifact_checks": []}],
                [],
                state_hash="state",
                out_root=str(tmp_path / "out"),
                judge_config=None,
            )


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

    def test_runtime_skills_use_frontmatter_names(self, tmp_path) -> None:
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        (first / "SKILL.md").write_text("---\nname: alpha\n---\n# A\n", encoding="utf-8")
        (second / "SKILL.md").write_text("---\nname: beta\n---\n# B\n", encoding="utf-8")
        skills = evaluate_skill.collect_runtime_skills([str(first), str(second)])
        assert [skill["name"] for skill in skills] == ["alpha", "beta"]

    def test_runtime_skills_reject_duplicate_names(self, tmp_path) -> None:
        first = tmp_path / "one"
        second = tmp_path / "two"
        for path in (first, second):
            path.mkdir()
            (path / "SKILL.md").write_text(
                "---\nname: duplicate\n---\n# Skill\n",
                encoding="utf-8",
            )
        with pytest.raises(ValueError, match="duplicate skill name"):
            evaluate_skill.collect_runtime_skills([str(first), str(second)])

    def test_runtime_skills_reject_unsafe_name(self, tmp_path) -> None:
        skill = tmp_path / "unsafe"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: ../escape\n---\n# Skill\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="filesystem-safe"):
            evaluate_skill.collect_runtime_skills([str(skill)])

    def test_standalone_markdown_uses_file_stem_as_runtime_name(self, tmp_path) -> None:
        skill = tmp_path / "standalone.md"
        skill.write_text("# Standalone\n", encoding="utf-8")
        runtime_skills = evaluate_skill.collect_runtime_skills([str(skill)])
        assert runtime_skills[0]["name"] == "standalone"


class TestEvaluateSkillCli:
    @staticmethod
    def _skill(tmp_path, name: str):
        skill = tmp_path / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n# {name}\n",
            encoding="utf-8",
        )
        return skill

    def test_single_skill_keeps_legacy_runtime_layout(
        self, tmp_path, monkeypatch
    ) -> None:
        skill = self._skill(tmp_path, "alpha")
        tasks = _write_tasks(tmp_path, [_valid_item()])
        out_root = tmp_path / "out"
        seen = {}
        args = SimpleNamespace(
            skill=[str(skill)],
            tasks=tasks,
            limit=0,
            out_root=str(out_root),
            workers=1,
            timeout=60,
            model="",
            **_JUDGE_CLI_DEFAULTS,
        )
        monkeypatch.setattr(evaluate_skill, "parse_args", lambda: args)
        monkeypatch.setattr(evaluate_skill, "_configure_backends", lambda _args: None)

        def fake_run_batch(items, skill_content, output, **kwargs):
            seen.update(
                items=items,
                skill_content=skill_content,
                output=output,
                kwargs=kwargs,
            )
            return [{"id": items[0]["id"], "response": "ok", "duration_s": 0.1,
                     "work_dir": "/nx", "artifacts": []}]

        monkeypatch.setattr(evaluate_skill, "run_batch", fake_run_batch)
        monkeypatch.setattr(
            evaluate_skill,
            "evaluate_rollouts",
            lambda items, rollouts, **kwargs: [_result(items[0]["id"], 1, 1.0)],
        )

        evaluate_skill.main()

        assert seen["kwargs"]["runtime_skills"] is None
        assert seen["skill_content"].endswith("# alpha\n")
        summary = json.loads((out_root / "summary.json").read_text(encoding="utf-8"))
        assert summary["mode"] == "skill"
        assert summary["skill_names"] == ["alpha"]

    def test_repeated_skills_share_one_rollout_and_write_plugin_summary(
        self, tmp_path, monkeypatch
    ) -> None:
        alpha = self._skill(tmp_path, "alpha")
        beta = self._skill(tmp_path, "beta")
        tasks = _write_tasks(
            tmp_path,
            [
                _valid_item(
                    target_skills=["alpha", "beta"],
                    task_type="integration",
                )
            ],
        )
        out_root = tmp_path / "plugin-out"
        seen = {}
        args = SimpleNamespace(
            skill=[str(alpha), str(beta)],
            tasks=tasks,
            limit=0,
            out_root=str(out_root),
            workers=1,
            timeout=60,
            model="",
            **_JUDGE_CLI_DEFAULTS,
        )
        monkeypatch.setattr(evaluate_skill, "parse_args", lambda: args)
        monkeypatch.setattr(evaluate_skill, "_configure_backends", lambda _args: None)

        def fake_run_batch(items, skill_content, output, **kwargs):
            seen["runtime_skills"] = kwargs["runtime_skills"]
            return [{"id": items[0]["id"], "response": "ok", "duration_s": 0.1,
                     "work_dir": "/nx", "artifacts": []}]

        monkeypatch.setattr(evaluate_skill, "run_batch", fake_run_batch)
        monkeypatch.setattr(
            evaluate_skill,
            "evaluate_rollouts",
            lambda items, rollouts, **kwargs: [_result(items[0]["id"], 1, 0.9)],
        )

        evaluate_skill.main()

        assert [skill["name"] for skill in seen["runtime_skills"]] == ["alpha", "beta"]
        summary = json.loads((out_root / "summary.json").read_text(encoding="utf-8"))
        assert summary["mode"] == "plugin"
        assert summary["skill_count"] == 2
        assert summary["integration"]["count"] == 1
        results = json.loads((out_root / "results.json").read_text(encoding="utf-8"))
        assert results[0]["target_skills"] == ["alpha", "beta"]

    def test_limit_does_not_hide_unknown_target(self, tmp_path, monkeypatch) -> None:
        alpha = self._skill(tmp_path, "alpha")
        beta = self._skill(tmp_path, "beta")
        tasks = _write_tasks(
            tmp_path,
            [
                _valid_item(target_skills=["alpha"]),
                _valid_item("late", target_skills=["unknown"]),
            ],
        )
        args = SimpleNamespace(
            skill=[str(alpha), str(beta)],
            tasks=tasks,
            limit=1,
        )
        monkeypatch.setattr(evaluate_skill, "parse_args", lambda: args)
        with pytest.raises(SystemExit, match="unknown skills"):
            evaluate_skill.main()


from skillopt.model.codex_harness import prepare_workspace  # noqa: E402


class TestPluginWorkspace:
    def test_materializes_separate_skills_with_support_files(self, tmp_path) -> None:
        source = tmp_path / "source.py"
        source.write_text("print('ok')", encoding="utf-8")
        work_dir = tmp_path / "work"

        skill_path, task_path = prepare_workspace(
            work_dir=str(work_dir),
            skill_md="unused",
            task_text="Do the task",
            installed_skills=[("alpha", "# Alpha"), ("beta", "# Beta")],
            copy_files=[
                (
                    str(source),
                    os.path.join(".agents", "skills", "beta", "scripts", "source.py"),
                )
            ],
        )

        assert skill_path == str(work_dir / ".agents" / "skills" / "alpha" / "SKILL.md")
        assert task_path == str(work_dir / "task.md")
        assert (work_dir / ".agents" / "skills" / "alpha" / "SKILL.md").read_text() == "# Alpha"
        assert (work_dir / ".agents" / "skills" / "beta" / "SKILL.md").read_text() == "# Beta"
        assert (
            work_dir / ".agents" / "skills" / "beta" / "scripts" / "source.py"
        ).read_text() == "print('ok')"

    @pytest.mark.parametrize(
        ("installed_skills", "copy_destination", "message"),
        [
            ([("alpha", "# A"), ("alpha", "# B")], None, "duplicate"),
            ([("../escape", "# A")], None, "invalid"),
            ([("alpha", "# A")], "../escape.txt", "safe relative"),
            (
                [("alpha", "# A")],
                os.path.join(".agents", "skills", "alpha", "SKILL.md"),
                "collides",
            ),
        ],
    )
    def test_invalid_layout_fails_before_existing_workspace_is_deleted(
        self,
        tmp_path,
        installed_skills,
        copy_destination,
        message,
    ) -> None:
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        sentinel = work_dir / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        source = tmp_path / "source.txt"
        source.write_text("source", encoding="utf-8")
        copy_files = [(str(source), copy_destination)] if copy_destination else None

        with pytest.raises(ValueError, match=message):
            prepare_workspace(
                work_dir=str(work_dir),
                skill_md="unused",
                installed_skills=installed_skills,
                copy_files=copy_files,
            )
        assert sentinel.read_text(encoding="utf-8") == "keep"

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
    @staticmethod
    def _ratio_round_trip(tmp_path, items, name):
        data_path = tmp_path / f"{name}.json"
        data_path.write_text(json.dumps(items), encoding="utf-8")
        split_dir = tmp_path / f"{name}-split"

        materializer = SkillEvalDataLoader(
            data_path=str(data_path),
            split_mode="ratio",
            split_ratio="1:1:1",
            split_output_dir=str(split_dir),
        )
        materializer.setup({})

        reloaded = SkillEvalDataLoader(
            split_dir=str(split_dir),
            split_mode="split_dir",
        )
        reloaded.setup({})
        loaded = reloaded.train_items + reloaded.val_items + reloaded.test_items
        serialized = []
        for split_name in ("train", "val", "test"):
            split_path = split_dir / split_name / "items.json"
            serialized.extend(json.loads(split_path.read_text(encoding="utf-8")))
        return loaded, serialized

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

    def test_ratio_split_reload_preserves_omitted_judge_mode(self, tmp_path) -> None:
        items = [
            _valid_item(f"implicit-{index}", _judge_mode_explicit=True)
            for index in range(3)
        ]
        loaded, serialized = self._ratio_round_trip(tmp_path, items, "implicit")

        assert all(item["judge_mode"] == "auto" for item in loaded)
        assert all(item["_judge_mode_explicit"] is False for item in loaded)
        assert all("judge_mode" not in item for item in serialized)
        assert all("_judge_mode_explicit" not in item for item in serialized)

    @pytest.mark.parametrize("mode", ["auto", "agentic", "chat"])
    def test_ratio_split_reload_preserves_explicit_judge_mode(
        self,
        tmp_path,
        mode,
    ) -> None:
        items = [
            _valid_item(
                f"{mode}-{index}",
                judge_mode=mode,
                _judge_mode_explicit=False,
            )
            for index in range(3)
        ]
        loaded, serialized = self._ratio_round_trip(tmp_path, items, mode)

        assert all(item["judge_mode"] == mode for item in loaded)
        assert all(item["_judge_mode_explicit"] is True for item in loaded)
        assert all(item["judge_mode"] == mode for item in serialized)
        assert all("_judge_mode_explicit" not in item for item in serialized)


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

    def test_persisted_trajectory_has_criteria_and_coverage_not_artifact_text(
        self, tmp_path, monkeypatch
    ) -> None:
        a = self._adapter(tmp_path)
        items = a.build_train_env(batch_size=2, seed=1)
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        raw_document_text = "RAW EXTRACTED DOCUMENT TEXT: quarterly revenue $42,000"
        (work_dir / "report.txt").write_text(raw_document_text, encoding="utf-8")

        def fake_run_batch(batch_items, skill, out_dir, **kw):
            return [
                {"id": batch_items[0]["id"], "task_type": "default",
                 "response": "answer A", "duration_s": 1.0, "work_dir": str(work_dir),
                 "artifacts": []},
                {"id": batch_items[1]["id"], "task_type": "default",
                 "response": "", "error": "boom", "duration_s": 0.1, "work_dir": str(work_dir)},
            ]

        def fake_evaluate_rollouts(items, rollout_results, **kwargs):
            merged = []
            for _item, result in zip(items, rollout_results):
                result = dict(result)
                if not result.get("error"):
                    result.update({
                        "hard": 1, "soft": 1.0, "judge_reason": "criteria satisfied",
                        "judge_status": "valid_pass",
                        "judge_criteria": [
                            {"id": "criterion_1", "passed": True, "score": 1.0,
                             "reason": "matched", "evidence": []},
                        ],
                        "judge_coverage": {
                            "artifacts": ["report.txt"],
                            "units_inspected": ["sheet1"],
                            "units_omitted": [],
                        },
                        "score_valid": True,
                    })
                merged.append(result)
            return merged

        monkeypatch.setattr(adapter_mod, "run_batch", fake_run_batch)
        monkeypatch.setattr(adapter_mod, "evaluate_rollouts", fake_evaluate_rollouts)

        out_dir = str(tmp_path / "rollout2")
        a.rollout(items, "# skill", out_dir)

        conv = json.loads(
            (tmp_path / "rollout2" / "predictions" / items[0]["id"] / "conversation.json").read_text()
        )
        note = conv[2]["content"]
        assert "criterion_1" in note
        assert "units_inspected" in note
        assert raw_document_text not in note

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
