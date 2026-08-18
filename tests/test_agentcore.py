"""Tests for the AgentCore remote exec runner and worker (no AWS calls)."""
from __future__ import annotations

import io
import json
import os
import tarfile

import pytest

from skillopt.model import agentcore_runner, agentcore_worker, codex_harness
from skillopt.model.agentcore_runner import AgentCoreRunnerError


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    """Dict-backed stand-in for the tiny S3 surface the runner/worker use."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[f"{Bucket}/{Key}"] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        key = f"{Bucket}/{Key}"
        if key not in self.objects:
            raise KeyError(key)
        return {"Body": FakeBody(self.objects[key])}

    def upload_file(self, path, Bucket, Key):
        with open(path, "rb") as f:
            self.objects[f"{Bucket}/{Key}"] = f.read()


class FakeRuntimeClient:
    """Routes invoke_agent_runtime straight into the real worker handler."""

    def __init__(self, s3: FakeS3):
        self.s3 = s3
        self.invocations: list[dict] = []
        self.stopped: list[str] = []

    def invoke_agent_runtime(self, *, agentRuntimeArn, qualifier, runtimeSessionId, payload):
        self.invocations.append(json.loads(payload))
        reply = agentcore_worker.handle_payload(json.loads(payload))
        return {"response": FakeBody(json.dumps(reply).encode())}

    def stop_runtime_session(self, **kwargs):
        self.stopped.append(kwargs["runtimeSessionId"])


@pytest.fixture()
def remote_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "agentcore")
    monkeypatch.setenv(
        "SKILLOPT_AGENTCORE_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/test-abc",
    )
    monkeypatch.setenv("SKILLOPT_AGENTCORE_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("SKILLOPT_AGENTCORE_SYNC_INTERVAL", "0")
    monkeypatch.setenv("SKILLOPT_AGENTCORE_POLL_INTERVAL", "0.05")
    s3 = FakeS3()
    runtime = FakeRuntimeClient(s3)
    monkeypatch.setattr(agentcore_runner, "_make_clients", lambda region, **kw: (runtime, s3))
    monkeypatch.setattr(agentcore_worker, "_s3_client", lambda: s3)
    # Worker work root must not collide between tests.
    monkeypatch.setattr(agentcore_worker, "_work_root", lambda: str(tmp_path / "worker-root"))
    os.makedirs(tmp_path / "worker-root", exist_ok=True)
    return runtime, s3


def _seed_work_dir(tmp_path):
    work_dir = tmp_path / "out" / "rollouts" / "task-1"
    work_dir.mkdir(parents=True)
    (work_dir / "task.md").write_text("do the thing", encoding="utf-8")
    return str(work_dir)


class TestRunnerConfig:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("SKILLOPT_EXEC_RUNNER", raising=False)
        assert agentcore_runner.is_enabled() is False

    def test_local_value(self, monkeypatch):
        monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "local")
        assert agentcore_runner.is_enabled() is False

    def test_unknown_value_raises(self, monkeypatch):
        monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "lambda")
        with pytest.raises(AgentCoreRunnerError):
            agentcore_runner.is_enabled()

    def test_missing_arn_fails_fast(self, monkeypatch):
        monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "agentcore")
        monkeypatch.delenv("SKILLOPT_AGENTCORE_RUNTIME_ARN", raising=False)
        monkeypatch.setenv("SKILLOPT_AGENTCORE_S3_BUCKET", "b")
        with pytest.raises(AgentCoreRunnerError, match="RUNTIME_ARN"):
            agentcore_runner.get_runner_config()

    def test_region_parsed_from_arn(self, monkeypatch):
        monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "agentcore")
        monkeypatch.setenv(
            "SKILLOPT_AGENTCORE_RUNTIME_ARN",
            "arn:aws:bedrock-agentcore:eu-west-1:111122223333:runtime/x",
        )
        monkeypatch.setenv("SKILLOPT_AGENTCORE_S3_BUCKET", "b")
        monkeypatch.delenv("SKILLOPT_AGENTCORE_REGION", raising=False)
        assert agentcore_runner.get_runner_config()["region"] == "eu-west-1"


class TestRemoteExecRoundTrip:
    def test_work_dir_round_trip(self, remote_env, tmp_path, monkeypatch):
        runtime, s3 = remote_env
        work_dir = _seed_work_dir(tmp_path)

        def fake_backend(backend, *, work_dir, prompt, model, timeout, **kwargs):
            with open(os.path.join(work_dir, "produced.txt"), "w", encoding="utf-8") as f:
                f.write("artifact")
            os.remove(os.path.join(work_dir, "task.md"))
            return "<answer>42</answer>", "===== CLAUDE CLI ATTEMPT 1 =====\nfake raw"

        monkeypatch.setattr(agentcore_worker, "_run_backend", fake_backend)

        response, raw = agentcore_runner.run_remote_exec(
            backend="claude_code_exec",
            work_dir=work_dir,
            prompt="solve",
            model="us.anthropic.claude-sonnet-5",
            timeout=60,
            exec_kwargs={"allowed_tools": "Read,Bash", "permission_mode": None, "allow_file_edits": True},
            exec_config={"effort": "medium", "max_thinking_tokens": 1024, "empty_response_retries": 1},
        )

        assert response == "<answer>42</answer>"
        assert "fake raw" in raw
        # Remote mutations replaced the local work_dir wholesale.
        assert os.path.exists(os.path.join(work_dir, "produced.txt"))
        assert not os.path.exists(os.path.join(work_dir, "task.md"))
        # Async contract: invoke was accepted, completion came via S3 polling,
        # and the session was stopped only after the result was fetched.
        assert runtime.stopped
        assert any(k.endswith("started.json") for k in s3.objects)
        sent = runtime.invocations[0]
        assert sent["backend"] == "claude_code_exec"
        assert sent["exec_kwargs"]["allow_file_edits"] is True
        assert "path" not in sent["exec_config"]

    def test_harness_exception_reraised_locally(self, remote_env, tmp_path, monkeypatch):
        work_dir = _seed_work_dir(tmp_path)

        def boom(backend, **kwargs):
            raise RuntimeError("codex exited 2")

        monkeypatch.setattr(agentcore_worker, "_run_backend", boom)
        with pytest.raises(RuntimeError, match="codex exited 2"):
            agentcore_runner.run_remote_exec(
                backend="codex_exec",
                work_dir=work_dir,
                prompt="p",
                model="openai.gpt-5.5",
                timeout=60,
                exec_kwargs={},
                exec_config={},
            )

    def test_images_rejected(self, remote_env, tmp_path):
        work_dir = _seed_work_dir(tmp_path)
        with pytest.raises(ValueError, match="images/data_dirs"):
            agentcore_runner.run_remote_exec(
                backend="claude_code_exec",
                work_dir=work_dir,
                prompt="p",
                model="m",
                timeout=60,
                exec_kwargs={},
                exec_config={},
                images=["/tmp/x.png"],
            )

    def test_poll_fallback_when_invoke_reply_lost(self, remote_env, tmp_path, monkeypatch):
        runtime, s3 = remote_env
        work_dir = _seed_work_dir(tmp_path)

        real_handle = agentcore_worker.handle_payload

        def flaky_invoke(*, agentRuntimeArn, qualifier, runtimeSessionId, payload):
            real_handle(json.loads(payload))  # job accepted and started...
            raise ConnectionError("read timeout")  # ...but the reply is lost

        monkeypatch.setattr(runtime, "invoke_agent_runtime", flaky_invoke)
        monkeypatch.setattr(
            agentcore_worker,
            "_run_backend",
            lambda backend, **kw: ("<answer>ok</answer>", "raw"),
        )
        response, raw = agentcore_runner.run_remote_exec(
            backend="claude_code_exec",
            work_dir=work_dir,
            prompt="p",
            model="m",
            timeout=60,
            exec_kwargs={},
            exec_config={},
        )
        assert response == "<answer>ok</answer>"

    def test_never_started_job_fails_fast(self, remote_env, tmp_path, monkeypatch):
        """Invoke fails AND no started.json appears -> error without waiting
        out the whole task deadline."""
        runtime, s3 = remote_env
        work_dir = _seed_work_dir(tmp_path)

        def dead_invoke(**kwargs):
            raise ConnectionError("boom")

        monkeypatch.setattr(runtime, "invoke_agent_runtime", dead_invoke)
        monkeypatch.setattr(agentcore_runner, "_poll_json", lambda *a, **k: None)
        with pytest.raises(agentcore_runner.AgentCoreRunnerError, match="never started"):
            agentcore_runner.run_remote_exec(
                backend="claude_code_exec",
                work_dir=work_dir,
                prompt="p",
                model="m",
                timeout=60,
                exec_kwargs={},
                exec_config={},
            )


class TestHarnessHook:
    def test_claude_exec_delegates_and_persists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "agentcore")
        work_dir = _seed_work_dir(tmp_path)
        calls = {}

        def fake_remote(**kwargs):
            calls.update(kwargs)
            return "<answer>hi</answer>", "===== CLAUDE CLI ATTEMPT 1 =====\nremote raw"

        monkeypatch.setattr(codex_harness.agentcore_runner, "run_remote_exec", fake_remote)
        response, raw = codex_harness.run_claude_code_exec(
            work_dir=work_dir,
            prompt="solve",
            model="m",
            timeout=30,
            allowed_tools="Read,Bash",
            allow_file_edits=True,
        )
        assert response == "<answer>hi</answer>"
        assert calls["backend"] == "claude_code_exec"
        assert calls["exec_kwargs"]["allow_file_edits"] is True
        # Artifact persistence matches the local path: raw lands in the parent dir.
        raw_path = os.path.join(os.path.dirname(work_dir), "claude_raw.txt")
        assert os.path.exists(raw_path)
        assert "remote raw" in open(raw_path, encoding="utf-8").read()

    def test_codex_exec_delegates(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "agentcore")
        work_dir = _seed_work_dir(tmp_path)
        calls = {}

        def fake_remote(**kwargs):
            calls.update(kwargs)
            return "resp", "===== CODEX CLI ATTEMPT 1 =====\nraw"

        monkeypatch.setattr(codex_harness.agentcore_runner, "run_remote_exec", fake_remote)
        response, _ = codex_harness.run_codex_exec(
            work_dir=work_dir, prompt="p", model="gpt-5.5", timeout=30, sandbox="workspace-write"
        )
        assert response == "resp"
        assert calls["backend"] == "codex_exec"
        assert calls["exec_kwargs"]["sandbox"] == "workspace-write"

    def test_judge_policy_never_remote(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "agentcore")

        def explode(**kwargs):
            raise AssertionError("judge must not go remote")

        monkeypatch.setattr(codex_harness.agentcore_runner, "run_remote_exec", explode)
        judged = {}

        def fake_judge(**kwargs):
            judged.update(kwargs)
            return "verdict", "raw"

        monkeypatch.setattr(codex_harness, "_run_claude_code_judge_cli", fake_judge)
        policy = {"judge": True}
        monkeypatch.setattr(codex_harness, "_is_judge_policy", lambda p: True)
        response, _ = codex_harness.run_claude_code_exec(
            work_dir=str(tmp_path), prompt="j", model="m", timeout=30, policy=policy
        )
        assert response == "verdict"
        assert judged["policy"] is policy


class TestWorkerPayloads:
    def test_ping(self, monkeypatch, tmp_path):
        monkeypatch.setattr(agentcore_worker, "_work_root", lambda: str(tmp_path))
        reply = agentcore_worker.handle_payload({"action": "ping"})
        assert reply["ok"] is True
        assert reply["work_root_writable"] is True

    def test_unknown_action(self):
        reply = agentcore_worker.handle_payload({"action": "nope"})
        assert reply["ok"] is False

    def test_invalid_backend_rejected_at_accept_time(self, monkeypatch, tmp_path):
        monkeypatch.setattr(agentcore_worker, "_work_root", lambda: str(tmp_path))
        reply = agentcore_worker.handle_payload(
            {"action": "exec", "backend": "qwen_chat", "bucket": "b", "job_prefix": "p/j1"}
        )
        assert reply["ok"] is False
        assert "Unsupported backend" in reply["error"]

    def test_job_failure_lands_in_result_json(self, monkeypatch, tmp_path):
        """Missing in.tar.gz: the background job must still flush result.json
        with the error, since it is the only completion channel."""
        s3 = FakeS3()
        monkeypatch.setattr(agentcore_worker, "_work_root", lambda: str(tmp_path))
        monkeypatch.setattr(agentcore_worker, "_s3_client", lambda: s3)
        reply = agentcore_worker.handle_payload(
            {"action": "exec", "backend": "claude_code_exec", "bucket": "b",
             "job_prefix": "p/j1", "wait": True}
        )
        assert reply["ok"] is True
        assert reply["harness_error"] is True
        assert "b/p/j1/started.json" in s3.objects
        result = json.loads(s3.objects["b/p/j1/result.json"])
        assert result["error"]

    def test_async_accept_then_background_flush(self, monkeypatch, tmp_path):
        import time as _time

        s3 = FakeS3()
        monkeypatch.setattr(agentcore_worker, "_work_root", lambda: str(tmp_path))
        monkeypatch.setattr(agentcore_worker, "_s3_client", lambda: s3)
        monkeypatch.setattr(agentcore_worker, "_run_backend", lambda backend, **kw: ("r", "raw"))
        buf = io.BytesIO()
        src = tmp_path / "seed-async"
        src.mkdir()
        (src / "task.md").write_text("t", encoding="utf-8")
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(src, arcname=agentcore_runner.TAR_ROOT)
        s3.objects["b/p/j3/in.tar.gz"] = buf.getvalue()

        reply = agentcore_worker.handle_payload(
            {"action": "exec", "backend": "claude_code_exec", "bucket": "b", "job_prefix": "p/j3"}
        )
        assert reply == {"ok": True, "accepted": True, "job_prefix": "p/j3"}
        deadline = _time.time() + 5
        while "b/p/j3/result.json" not in s3.objects and _time.time() < deadline:
            _time.sleep(0.02)
        assert "b/p/j3/result.json" in s3.objects
        assert "b/p/j3/out.tar.gz" in s3.objects

    def test_exec_config_applied(self, remote_env, tmp_path, monkeypatch):
        from skillopt.model import backend_config

        _runtime, s3 = remote_env
        buf = io.BytesIO()
        src = tmp_path / "seed"
        src.mkdir()
        (src / "task.md").write_text("t", encoding="utf-8")
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(src, arcname=agentcore_runner.TAR_ROOT)
        s3.objects["b/p/j2/in.tar.gz"] = buf.getvalue()
        monkeypatch.setattr(agentcore_worker, "_run_backend", lambda backend, **kw: ("r", "raw"))
        reply = agentcore_worker.handle_payload(
            {
                "action": "exec",
                "backend": "claude_code_exec",
                "bucket": "b",
                "job_prefix": "p/j2",
                "exec_config": {"effort": "high", "empty_response_retries": 3},
                "wait": True,
            }
        )
        assert reply["ok"] is True
        assert backend_config.CLAUDE_CODE_EXEC_USE_SDK == "cli"
        assert backend_config.CLAUDE_CODE_EXEC_EFFORT == "high"
        assert backend_config.EXEC_EMPTY_RESPONSE_RETRIES == 3
        assert "b/p/j2/result.json" in s3.objects
        assert "b/p/j2/out.tar.gz" in s3.objects
