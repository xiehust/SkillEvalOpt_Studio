"""Token usage extraction from exec backend raw transcripts (ReflACT harness)."""
from __future__ import annotations

import json
import subprocess

import pytest

from skillopt.model import codex_harness
from skillopt.model.codex_harness import extract_exec_failure, extract_exec_usage


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


class TestExtractExecFailure:
    def test_collects_claude_api_errors_by_attempt(self) -> None:
        raw = (
            "===== CLAUDE CLI ATTEMPT 1 =====\n"
            f"{_cli_json('API Error: Response stalled mid-stream', is_error=True)}\n\n"
            "===== CLAUDE CLI ATTEMPT 2 =====\n"
            f"{_cli_json('API Error: overloaded', is_error=True)}"
        )
        assert extract_exec_failure(raw) == (
            "CLAUDE CLI attempt 1: API Error: Response stalled mid-stream; "
            "CLAUDE CLI attempt 2: API Error: overloaded"
        )

    def test_empty_or_unstructured_raw_has_no_failure(self) -> None:
        assert extract_exec_failure("") is None
        assert extract_exec_failure("plain text output") is None


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
        assert extract_exec_failure(raw) == (
            "claude_code_exec: process exited with code 1: boom"
        )

    def test_timeout_reason_is_preserved(self, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(codex_harness.subprocess, "run", fake_run)
        response, raw = codex_harness._run_claude_code_cli_exec(
            work_dir="/tmp", prompt="p", model="m", timeout=5,
        )
        assert response == ""
        assert extract_exec_failure(raw) == (
            "claude_code_exec: timed out after 5 seconds"
        )


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


_ARTIFACT_TOOLS = ("artifact_inventory", "artifact_inspect", "artifact_render", "artifact_extract")
_BUILTIN_TOOLS = ("Read", "Bash", "Edit", "Write", "WebSearch", "WebFetch")


def _judge_policy(tmp_path, backend: str):
    """A judge policy built through the real agentic_judge helpers."""
    from skillopt.envs.skilleval.agentic_judge import build_backend_policy

    mcp_command = [
        "/usr/bin/bwrap",
        "--unshare-net",
        "python3",
        "-m",
        "skillopt.envs.skilleval.artifact_mcp",
    ]
    return build_backend_policy(backend, mcp_command, str(tmp_path))


class TestJudgeExecPolicyFailClosed:
    """The per-call judge policy must be fail-closed and expose only the MCP."""

    def test_claude_judge_cli_announces_only_artifact_mcp_tools(self, tmp_path, monkeypatch) -> None:
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout=_cli_json('{"schema_version": 1}'), stderr="")

        monkeypatch.setattr(codex_harness.subprocess, "run", fake_run)
        policy = _judge_policy(tmp_path, "claude_code_exec")
        response, _raw = codex_harness.run_claude_code_exec(
            work_dir=str(tmp_path), prompt="judge please", model="m", timeout=5, policy=policy,
        )
        cmd = captured["cmd"]
        # No built-in tools are enabled and only the Artifact MCP tools are allowed.
        assert cmd[cmd.index("--tools") + 1] == ""
        allowed = cmd[cmd.index("--allowedTools") + 1]
        assert allowed == ",".join(f"mcp__artifactctl__{name}" for name in _ARTIFACT_TOOLS)
        for builtin in _BUILTIN_TOOLS:
            assert builtin not in allowed
        assert "--strict-mcp-config" in cmd
        assert cmd[cmd.index("--setting-sources") + 1] == ""
        # The rollout / evidence directories are never added.
        assert "--add-dir" not in cmd
        # Structured output uses the real claude CLI flag `--json-schema`; the
        # earlier `--schema` does not exist in the CLI and made every judgment
        # exit non-zero. Asserting the exact flag here breaks on future drift.
        assert "--schema" not in cmd
        assert "--json-schema" in cmd
        assert json.loads(cmd[cmd.index("--json-schema") + 1]) == policy["output_schema"]
        assert response == '{"schema_version": 1}'

    def test_codex_judge_cli_is_read_only_with_only_the_mcp(self, tmp_path, monkeypatch) -> None:
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            index = cmd.index("--output-last-message")
            with open(cmd[index + 1], "w", encoding="utf-8") as handle:
                handle.write('{"schema_version": 1}')
            return subprocess.CompletedProcess(cmd, 0, stdout="tokens used\n42\n", stderr="")

        monkeypatch.setattr(codex_harness.subprocess, "run", fake_run)
        policy = _judge_policy(tmp_path, "codex_exec")
        response, _raw = codex_harness.run_codex_exec(
            work_dir=str(tmp_path), prompt="judge please", model="m", timeout=5, policy=policy,
        )
        cmd = captured["cmd"]
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert 'approval_policy="never"' in cmd
        assert "--ephemeral" in cmd
        assert "project_doc_max_bytes=0" in cmd
        assert "tools.web_search=false" in cmd
        assert any(part.startswith("mcp_servers.artifactctl.command=") for part in cmd)
        assert any(part.startswith("mcp_servers.artifactctl.args=") for part in cmd)
        # No built-in shell / web tool is announced anywhere in the command.
        for builtin in _BUILTIN_TOOLS:
            assert not any(builtin in part for part in cmd)
        # User config is ignored via an isolated CODEX_HOME.
        assert captured["kwargs"]["env"]["CODEX_HOME"].endswith(".codex_home")
        assert response == '{"schema_version": 1}'

    def test_non_judge_policy_is_rejected_fail_closed(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            codex_harness.run_claude_code_exec(
                work_dir=str(tmp_path), prompt="p", model="m", timeout=5, policy={"judge": False},
            )
        with pytest.raises(ValueError):
            codex_harness.run_codex_exec(
                work_dir=str(tmp_path), prompt="p", model="m", timeout=5, policy={},
            )

    def test_judge_policy_requires_exactly_one_required_mcp(self, tmp_path) -> None:
        policy = dict(_judge_policy(tmp_path, "claude_code_exec"))
        policy["mcp_servers"] = {}
        with pytest.raises(RuntimeError):
            codex_harness.run_claude_code_exec(
                work_dir=str(tmp_path), prompt="p", model="m", timeout=5, policy=policy,
            )

    def test_default_target_runner_ignores_policy_kw_when_absent(self, monkeypatch, tmp_path) -> None:
        # Passing no policy keeps the existing CLI path byte-for-byte.
        def fake_run(cmd, **kwargs):
            assert "--strict-mcp-config" not in cmd
            assert "--sandbox" not in cmd or cmd[cmd.index("--sandbox") + 1] != "read-only"
            return subprocess.CompletedProcess(cmd, 0, stdout=_cli_json("plain"), stderr="")

        monkeypatch.setattr(codex_harness.subprocess, "run", fake_run)
        response, _raw = codex_harness._run_claude_code_cli_exec(
            work_dir=str(tmp_path), prompt="p", model="m", timeout=5,
        )
        assert response == "plain"


def _fake_claude_exe(tmp_path, *, mode: str):
    """Write a fake `claude` executable that mimics commander flag handling.

    ``--version`` always succeeds; the judge argv is then handled per ``mode``:
    ``reject`` errors like the CLI would on an unknown/renamed flag, ``accept``
    parses cleanly and exits 0, ``hang`` sleeps (simulating a valid argv that
    reaches the endpoint and blocks) so the flag check times out.
    """
    version_ok = "if '--version' in sys.argv:\n    print('9.9.9 (fake)')\n    sys.exit(0)\n"
    if mode == "reject":
        body = version_ok + "sys.stderr.write(\"error: unknown option '--json-schema'\\n\")\nsys.exit(1)\n"
    elif mode == "accept":
        body = version_ok + "sys.exit(0)\n"
    elif mode == "hang":
        body = version_ok + "import time\ntime.sleep(30)\n"
    else:  # pragma: no cover - test helper guard
        raise ValueError(mode)
    script = tmp_path / f"fake-claude-{mode}"
    script.write_text("#!/usr/bin/env python3\nimport sys\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


class TestClaudeJudgeFlagPreflight:
    """The token-free judge-argv flag check catches unknown/renamed CLI flags."""

    def test_rejects_a_cli_that_errors_on_the_judge_flags(self, tmp_path, monkeypatch) -> None:
        exe = _fake_claude_exe(tmp_path, mode="reject")
        monkeypatch.setattr(
            codex_harness, "get_claude_code_exec_config", lambda: {"path": str(exe), "effort": "low"}
        )
        policy = _judge_policy(tmp_path, "claude_code_exec")
        with pytest.raises(RuntimeError, match="judge policy flag"):
            codex_harness.check_claude_judge_cli_flags(policy=policy, model="m", timeout=10.0)

    def test_accepts_a_cli_that_parses_the_judge_flags(self, tmp_path, monkeypatch) -> None:
        exe = _fake_claude_exe(tmp_path, mode="accept")
        monkeypatch.setattr(
            codex_harness, "get_claude_code_exec_config", lambda: {"path": str(exe), "effort": "low"}
        )
        policy = _judge_policy(tmp_path, "claude_code_exec")
        codex_harness.check_claude_judge_cli_flags(policy=policy, model="m", timeout=10.0)

    def test_treats_a_hang_as_valid_flags(self, tmp_path, monkeypatch) -> None:
        exe = _fake_claude_exe(tmp_path, mode="hang")
        monkeypatch.setattr(
            codex_harness, "get_claude_code_exec_config", lambda: {"path": str(exe), "effort": "low"}
        )
        policy = _judge_policy(tmp_path, "claude_code_exec")
        codex_harness.check_claude_judge_cli_flags(policy=policy, model="m", timeout=0.5)


class TestJudgeWorkerProtocol:
    """judge_worker.run_worker_request drives the runner with the judge policy."""

    def test_worker_invokes_runner_with_policy_and_returns_usage(self, tmp_path, monkeypatch) -> None:
        from skillopt.envs.skilleval import judge_worker

        captured: dict = {}
        raw = json.dumps({
            "type": "result",
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 50,
            },
        })

        def fake_runner(**kwargs):
            captured.update(kwargs)
            return "VERDICT-JSON", raw

        monkeypatch.setattr(judge_worker, "run_claude_code_exec", fake_runner)
        monkeypatch.setattr(judge_worker, "configure_claude_code_exec", lambda **kwargs: None)
        policy = {"judge": True, "mcp_servers": {"artifactctl": {"required": True, "command": ["x"]}}}
        request = {
            "backend": "claude_code_exec",
            "model": "m",
            "effort": "low",
            "timeout": 30,
            "judge_client_dir": str(tmp_path),
            "prompt": "judge please",
            "backend_policy": policy,
        }
        out = judge_worker.run_worker_request(request)
        assert out["response"] == "VERDICT-JSON"
        assert out["usage"] == {"input": 100, "output": 50}
        assert captured["policy"] is policy
        assert captured["work_dir"] == str(tmp_path)
        assert captured["images"] == [] and captured["data_dirs"] == []

    def test_worker_rejects_request_without_judge_policy(self, tmp_path) -> None:
        from skillopt.envs.skilleval import judge_worker

        with pytest.raises(ValueError):
            judge_worker.run_worker_request({
                "backend": "claude_code_exec",
                "timeout": 5,
                "judge_client_dir": str(tmp_path),
                "prompt": "p",
                "backend_policy": {"judge": False},
            })
