"""Job-level token aggregation (artifacts.job_tokens) and the jobs API tokens field."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillopt_studio import artifacts
from skillopt_studio.app import create_app
from skillopt_studio.config import StudioConfig
from skillopt_studio.models import JobInfo

SOURCES = ("claude", "codex", "kiro", "agents")


@pytest.fixture
def studio_config(tmp_path: Path) -> StudioConfig:
    return StudioConfig(
        studio_root=tmp_path / "studio",
        skill_sources={name: tmp_path / "sources" / name for name in SOURCES},
    )


@pytest.fixture(autouse=True)
def clear_tokens_cache():
    artifacts._TOKENS_CACHE.clear()
    yield
    artifacts._TOKENS_CACHE.clear()


def make_job(config: StudioConfig, job_id: str, job_type: str, status: str = "succeeded") -> JobInfo:
    job_dir = config.jobs_dir / job_id
    (job_dir / "out").mkdir(parents=True, exist_ok=True)
    job = JobInfo(
        id=job_id, type=job_type, status=status,
        created_at="2026-07-09T00:00:00+00:00",
        out_root=str(job_dir / "out"),
    )
    (job_dir / "job.json").write_text(
        json.dumps(job.model_dump(exclude={"tokens"})), encoding="utf-8"
    )
    return job


def eval_rows() -> list[dict]:
    return [
        {"id": "t1", "hard": 1, "soft": 1.0,
         "usage": {"input": 100, "cache_write": 20, "cache_read": 300, "output": 50, "total": 470},
         "judge_usage": {"input": 800, "output": 40}},
        {"id": "t2", "hard": 0, "soft": 0.2,
         "usage": {"input": 10, "cache_write": 0, "cache_read": 0, "output": 5, "total": 15},
         "judge_usage": {"input": 700, "output": 30}},
    ]


class TestJobTokens:
    def test_eval_sums_exec_and_judge_usage(self, studio_config) -> None:
        job = make_job(studio_config, "eval-1", "eval")
        out = Path(job.out_root)
        (out / "results.json").write_text(json.dumps(eval_rows()), encoding="utf-8")
        tokens = artifacts.job_tokens(studio_config, job)
        assert tokens == {
            "input": 100 + 10 + 800 + 700,
            "cache_write": 20,
            "cache_read": 300,
            "output": 50 + 5 + 40 + 30,
            "total": 470 + 15 + (800 + 40) + (700 + 30),
        }

    def test_eval_rows_without_usage_returns_none(self, studio_config) -> None:
        job = make_job(studio_config, "eval-2", "eval")
        (Path(job.out_root) / "results.json").write_text(
            json.dumps([{"id": "t1", "hard": 1, "soft": 1.0}]), encoding="utf-8"
        )
        assert artifacts.job_tokens(studio_config, job) is None

    def test_train_maps_token_summary_total(self, studio_config) -> None:
        job = make_job(studio_config, "train-1", "train")
        summary = {"token_summary": {"_total": {
            "calls": 11, "prompt_tokens": 15000, "completion_tokens": 4000, "total_tokens": 19000,
        }}}
        (Path(job.out_root) / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        assert artifacts.job_tokens(studio_config, job) == {
            "input": 15000, "cache_write": 0, "cache_read": 0, "output": 4000, "total": 19000,
        }

    def test_train_adds_rollout_raws_to_optimizer_side(self, studio_config) -> None:
        job = make_job(studio_config, "train-2", "train")
        out = Path(job.out_root)
        summary = {"token_summary": {"_total": {
            "calls": 11, "prompt_tokens": 15000, "completion_tokens": 4000, "total_tokens": 19000,
        }}}
        (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        cli_json = json.dumps({
            "type": "result", "is_error": False, "result": "done",
            "usage": {"input_tokens": 200, "cache_creation_input_tokens": 30,
                      "cache_read_input_tokens": 500, "output_tokens": 70},
        })
        # rollout raws scattered across step/eval subdirectories (train layout)
        for rel in ("selection_eval_baseline/rollouts", "steps/step_0001/rollout/rollouts"):
            raw_dir = out / rel
            raw_dir.mkdir(parents=True)
            (raw_dir / "claude_raw.txt").write_text(
                f"===== CLAUDE CLI ATTEMPT 1 =====\n{cli_json}", encoding="utf-8"
            )
        # codex raw with total-only accounting also counts
        codex_dir = out / "steps/step_0002/rollout/rollouts"
        codex_dir.mkdir(parents=True)
        (codex_dir / "codex_raw.txt").write_text(
            "===== CODEX CLI ATTEMPT 1 =====\ncodex\nok\ntokens used\n1,000\n", encoding="utf-8"
        )
        per_raw = 200 + 30 + 500 + 70  # 800
        assert artifacts.job_tokens(studio_config, job) == {
            "input": 15000 + 200 * 2,
            "cache_write": 30 * 2,
            "cache_read": 500 * 2,
            "output": 4000 + 70 * 2,
            "total": 19000 + per_raw * 2 + 1000,
        }

    def test_train_rollout_raws_without_summary(self, studio_config) -> None:
        job = make_job(studio_config, "train-3", "train")
        out = Path(job.out_root)
        cli_json = json.dumps({
            "type": "result", "is_error": False, "result": "done",
            "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0, "output_tokens": 50},
        })
        raw_dir = out / "steps/step_0001/rollout/rollouts"
        raw_dir.mkdir(parents=True)
        (raw_dir / "claude_raw.txt").write_text(
            f"===== CLAUDE CLI ATTEMPT 1 =====\n{cli_json}", encoding="utf-8"
        )
        assert artifacts.job_tokens(studio_config, job) == {
            "input": 100, "cache_write": 0, "cache_read": 0, "output": 50, "total": 150,
        }

    def test_train_without_any_source_returns_none(self, studio_config) -> None:
        job = make_job(studio_config, "train-4", "train")
        assert artifacts.job_tokens(studio_config, job) is None

    def test_taskgen_extracts_from_raw(self, studio_config) -> None:
        job = make_job(studio_config, "taskgen-1", "taskgen")
        cli_json = json.dumps({
            "type": "result", "is_error": False, "result": "done",
            "usage": {"input_tokens": 50, "cache_creation_input_tokens": 5,
                      "cache_read_input_tokens": 10, "output_tokens": 25},
        })
        (Path(job.out_root) / "claude_raw.txt").write_text(
            f"===== CLAUDE CLI ATTEMPT 1 =====\n{cli_json}", encoding="utf-8"
        )
        assert artifacts.job_tokens(studio_config, job) == {
            "input": 50, "cache_write": 5, "cache_read": 10, "output": 25, "total": 90,
        }

    def test_no_artifacts_returns_none(self, studio_config) -> None:
        job = make_job(studio_config, "eval-3", "eval")
        assert artifacts.job_tokens(studio_config, job) is None

    def test_running_job_returns_none_without_io(self, studio_config, monkeypatch) -> None:
        job = make_job(studio_config, "eval-4", "eval", status="running")
        (Path(job.out_root) / "results.json").write_text(json.dumps(eval_rows()), encoding="utf-8")

        def boom(path):
            raise AssertionError("running job must not read artifacts")

        monkeypatch.setattr(artifacts, "_read_json", boom)
        assert artifacts.job_tokens(studio_config, job) is None

    def test_echo_type_returns_none(self, studio_config) -> None:
        job = make_job(studio_config, "echo-1", "echo")
        assert artifacts.job_tokens(studio_config, job) is None

    def test_finished_job_result_is_cached(self, studio_config, monkeypatch) -> None:
        job = make_job(studio_config, "eval-5", "eval")
        (Path(job.out_root) / "results.json").write_text(json.dumps(eval_rows()), encoding="utf-8")
        first = artifacts.job_tokens(studio_config, job)
        assert first is not None

        calls = {"n": 0}
        real = artifacts._read_json

        def counting(path):
            calls["n"] += 1
            return real(path)

        monkeypatch.setattr(artifacts, "_read_json", counting)
        assert artifacts.job_tokens(studio_config, job) == first
        assert calls["n"] == 0

    def test_none_result_is_cached_too(self, studio_config, monkeypatch) -> None:
        job = make_job(studio_config, "eval-6", "eval")
        assert artifacts.job_tokens(studio_config, job) is None

        def boom(path):
            raise AssertionError("cached None must not re-read")

        monkeypatch.setattr(artifacts, "_read_json", boom)
        assert artifacts.job_tokens(studio_config, job) is None

    def test_cache_bounded(self, studio_config) -> None:
        for i in range(artifacts._TOKENS_CACHE_MAX + 10):
            job = make_job(studio_config, f"eval-cache-{i}", "eval")
            artifacts.job_tokens(studio_config, job)
        assert len(artifacts._TOKENS_CACHE) <= artifacts._TOKENS_CACHE_MAX


class TestEvalResultsTokens:
    def test_summary_carries_tokens(self, studio_config) -> None:
        job = make_job(studio_config, "eval-7", "eval")
        (Path(job.out_root) / "results.json").write_text(json.dumps(eval_rows()), encoding="utf-8")
        results = artifacts.eval_results(studio_config, job)
        assert results["summary"]["tokens"]["total"] == 470 + 15 + 840 + 730
        assert results["rows"][0]["usage"]["input"] == 100
        assert results["rows"][0]["judge_usage"] == {"input": 800, "output": 40}

    def test_summary_tokens_none_without_usage(self, studio_config) -> None:
        job = make_job(studio_config, "eval-8", "eval")
        (Path(job.out_root) / "results.json").write_text(
            json.dumps([{"id": "t1", "hard": 1, "soft": 1.0}]), encoding="utf-8"
        )
        results = artifacts.eval_results(studio_config, job)
        assert results["summary"]["tokens"] is None


class TestJobsApiTokens:
    @pytest.fixture
    def client(self, studio_config):
        app = create_app(studio_config)
        with TestClient(app) as test_client:
            yield test_client

    def test_list_rows_carry_tokens_dict_and_null(self, studio_config, client) -> None:
        with_usage = make_job(studio_config, "eval-api-1", "eval")
        (Path(with_usage.out_root) / "results.json").write_text(
            json.dumps(eval_rows()), encoding="utf-8"
        )
        make_job(studio_config, "eval-api-2", "eval")  # finished, no artifacts

        response = client.get("/api/jobs")
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()}
        assert rows["eval-api-1"]["tokens"]["total"] == 470 + 15 + 840 + 730
        assert rows["eval-api-2"]["tokens"] is None

    def test_detail_carries_tokens(self, studio_config, client) -> None:
        job = make_job(studio_config, "eval-api-3", "eval")
        (Path(job.out_root) / "results.json").write_text(json.dumps(eval_rows()), encoding="utf-8")
        response = client.get("/api/jobs/eval-api-3")
        assert response.status_code == 200
        assert response.json()["tokens"]["input"] == 1610
