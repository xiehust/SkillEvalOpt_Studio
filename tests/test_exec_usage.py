"""Token usage extraction from exec backend raw transcripts (ReflACT harness)."""
from __future__ import annotations

import json
import subprocess

import pytest

from skillopt.model import codex_harness
from skillopt.model.codex_harness import extract_exec_usage


def _cli_json(result: str = "hello", *, is_error: bool = False,
              input_tokens: int = 100, cache_write: int = 20,
              cache_read: int = 300, output_tokens: int = 50) -> str:
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": result,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output_tokens,
            "service_tier": "standard",
        },
        "total_cost_usd": 0.01,
        "duration_ms": 1234,
    })


class TestExtractExecUsage:
    def test_cli_json_four_way_breakdown(self) -> None:
        raw = f"===== CLAUDE CLI ATTEMPT 1 =====\n{_cli_json()}"
        usage = extract_exec_usage(raw)
        assert usage == {"input": 100, "cache_write": 20, "cache_read": 300,
                         "output": 50, "total": 470}
        assert usage["total"] == usage["input"] + usage["cache_write"] + usage["cache_read"] + usage["output"]

    def test_legacy_text_raw_returns_none(self) -> None:
        raw = ("===== CLAUDE CLI ATTEMPT 1 =====\n"
               "I created summary.xlsx from data/region.csv.\n\n"
               "**Total: 1200**\n")
        assert extract_exec_usage(raw) is None

    def test_empty_raw_returns_none(self) -> None:
        assert extract_exec_usage("") is None
        assert extract_exec_usage("   \n") is None

    def test_codex_comma_grouped_total(self) -> None:
        raw = ("===== CODEX CLI ATTEMPT 1 =====\n"
               "codex\n2 + 2 = 4\ntokens used\n15,804\n")
        usage = extract_exec_usage(raw)
        assert usage == {"input": 0, "cache_write": 0, "cache_read": 0,
                         "output": 0, "total": 15804}

    def test_multi_segment_accumulates(self) -> None:
        raw = (f"===== CLAUDE CLI ATTEMPT 1 =====\n{_cli_json()}\n\n"
               f"===== TURN BREAK =====\n\n"
               f"===== CLAUDE CLI ATTEMPT 1 =====\n{_cli_json(input_tokens=10, cache_write=0, cache_read=0, output_tokens=5)}")
        usage = extract_exec_usage(raw)
        assert usage["input"] == 110
        assert usage["output"] == 55
        assert usage["total"] == 470 + 15

    def test_mixed_codex_turns_accumulate_total(self) -> None:
        raw = ("===== CODEX CLI ATTEMPT 1 =====\ncodex\nfoo\ntokens used\n1,000\n\n"
               "===== TURN BREAK =====\n\n"
               "===== CODEX CLI ATTEMPT 1 =====\ncodex\nbar\ntokens used\n2,500\n")
        usage = extract_exec_usage(raw)
        assert usage["total"] == 3500

    def test_never_raises_on_garbage(self) -> None:
        assert extract_exec_usage('{"usage": {"input_tokens": "not-a-number"}}') is None
        assert extract_exec_usage("tokens used\nnot a number") is None


class TestClaudeCliJsonMode:
    """_run_claude_code_cli_exec: JSON parsing with text fallback semantics."""

    def _run(self, monkeypatch, stdout: str, returncode: int = 0, stderr: str = "") -> tuple[str, str]:
        def fake_run(cmd, **kwargs):
            assert "--output-format" in cmd
            assert cmd[cmd.index("--output-format") + 1] == "json"
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(codex_harness.subprocess, "run", fake_run)
        return codex_harness._run_claude_code_cli_exec(
            work_dir="/tmp", prompt="p", model="m", timeout=5,
        )

    def test_json_result_becomes_response(self, monkeypatch) -> None:
        response, raw = self._run(monkeypatch, _cli_json("the answer"))
        assert response == "the answer"
        assert extract_exec_usage(raw) is not None

    def test_is_error_json_treated_as_empty_response(self, monkeypatch) -> None:
        stdout = _cli_json("API Error: 400 bad model", is_error=True)
        response, raw = self._run(monkeypatch, stdout)
        assert response == ""            # engages the empty-response retry loop
        assert "API Error" in raw        # but the evidence stays in raw

    def test_non_json_stdout_falls_back_to_text(self, monkeypatch) -> None:
        response, _raw = self._run(monkeypatch, "plain text answer\n")
        assert response == "plain text answer"

    def test_json_with_leading_warning_line(self, monkeypatch) -> None:
        stdout = "some warning line\n" + _cli_json("ok")
        response, _raw = self._run(monkeypatch, stdout)
        assert response == "ok"

    def test_nonzero_exit_empty_stdout(self, monkeypatch) -> None:
        response, raw = self._run(monkeypatch, "", returncode=1, stderr="boom")
        assert response == ""
        assert "boom" in raw


class TestRolloutUsageField:
    def test_rollout_result_carries_usage(self, tmp_path, monkeypatch) -> None:
        from skillopt.envs.skilleval import rollout

        raw = f"===== CLAUDE CLI ATTEMPT 1 =====\n{_cli_json('done')}"
        monkeypatch.setattr(rollout, "run_claude_code_exec",
                            lambda **kwargs: ("done", raw))
        monkeypatch.setattr(rollout, "get_target_backend", lambda: "claude_code_exec")
        item = {"id": "t1", "question": "q", "rubric": "r"}
        results = rollout.run_batch([item], "skill", str(tmp_path), workers=1, timeout=5)
        assert results[0]["usage"] == {"input": 100, "cache_write": 20,
                                       "cache_read": 300, "output": 50, "total": 470}

    def test_rollout_without_usage_omits_field(self, tmp_path, monkeypatch) -> None:
        from skillopt.envs.skilleval import rollout

        monkeypatch.setattr(rollout, "run_claude_code_exec",
                            lambda **kwargs: ("done", "plain text raw"))
        monkeypatch.setattr(rollout, "get_target_backend", lambda: "claude_code_exec")
        item = {"id": "t1", "question": "q", "rubric": "r"}
        results = rollout.run_batch([item], "skill", str(tmp_path), workers=1, timeout=5)
        assert "usage" not in results[0]


class TestJudgeUsageField:
    def test_judge_records_usage(self, monkeypatch) -> None:
        from skillopt.envs.skilleval import evaluator

        verdict = json.dumps({"pass": True, "score": 1.0, "reason": "ok"})
        monkeypatch.setattr(evaluator, "chat_optimizer",
                            lambda **kwargs: (verdict, {"prompt_tokens": 800, "completion_tokens": 40}))
        result = evaluator.judge({"id": "t1", "question": "q", "rubric": "r"}, "resp")
        assert result["hard"] == 1
        assert result["judge_usage"] == {"input": 800, "output": 40}

    def test_judge_usage_accumulates_over_retry(self, monkeypatch) -> None:
        from skillopt.envs.skilleval import evaluator

        replies = iter([
            ("not json", {"prompt_tokens": 100, "completion_tokens": 10}),
            (json.dumps({"pass": False, "score": 0.2, "reason": "meh"}),
             {"prompt_tokens": 120, "completion_tokens": 15}),
        ])
        monkeypatch.setattr(evaluator, "chat_optimizer", lambda **kwargs: next(replies))
        result = evaluator.judge({"id": "t1", "question": "q", "rubric": "r"}, "resp")
        assert result["judge_usage"] == {"input": 220, "output": 25}

    def test_judge_call_failure_omits_usage(self, monkeypatch) -> None:
        from skillopt.envs.skilleval import evaluator

        def boom(**kwargs):
            raise RuntimeError("down")

        monkeypatch.setattr(evaluator, "chat_optimizer", boom)
        result = evaluator.judge({"id": "t1", "question": "q", "rubric": "r"}, "resp")
        assert "judge_usage" not in result
        assert "judge_error" in result

    @pytest.mark.parametrize("usage", [None, "oops", 42])
    def test_non_dict_usage_tolerated(self, monkeypatch, usage) -> None:
        from skillopt.envs.skilleval import evaluator

        verdict = json.dumps({"pass": True, "score": 1.0, "reason": "ok"})
        monkeypatch.setattr(evaluator, "chat_optimizer", lambda **kwargs: (verdict, usage))
        result = evaluator.judge({"id": "t1", "question": "q", "rubric": "r"}, "resp")
        assert result["hard"] == 1
        assert result["judge_usage"] == {"input": 0, "output": 0}
