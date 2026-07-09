"""Dashboard aggregation endpoint: resources / skill_health / train_gains / failures / token_stats."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillopt_studio import artifacts
from skillopt_studio.api import dashboard as dashboard_api
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
def clear_caches():
    artifacts._TOKENS_CACHE.clear()
    dashboard_api._PASS_RATE_CACHE.clear()
    yield
    artifacts._TOKENS_CACHE.clear()
    dashboard_api._PASS_RATE_CACHE.clear()


@pytest.fixture
def client(studio_config):
    app = create_app(studio_config)
    with TestClient(app) as test_client:
        yield test_client


def write_job(
    config: StudioConfig,
    job_id: str,
    job_type: str,
    status: str = "succeeded",
    params: dict | None = None,
    created_at: str = "2026-07-01T00:00:00+00:00",
) -> JobInfo:
    job_dir = config.jobs_dir / job_id
    (job_dir / "out").mkdir(parents=True, exist_ok=True)
    job = JobInfo(
        id=job_id, type=job_type, status=status, created_at=created_at,
        finished_at=created_at, params=params or {}, out_root=str(job_dir / "out"),
    )
    (job_dir / "job.json").write_text(
        json.dumps(job.model_dump(exclude={"tokens"})), encoding="utf-8"
    )
    return job


def write_eval_results(job: JobInfo, hard_values: list[int], with_usage: bool = True) -> None:
    rows = []
    for index, hard in enumerate(hard_values):
        row: dict = {"id": f"t{index}", "hard": hard, "soft": float(hard)}
        if with_usage:
            row["usage"] = {"input": 100, "cache_write": 10, "cache_read": 50, "output": 20, "total": 180}
            row["judge_usage"] = {"input": 300, "output": 15}
        rows.append(row)
    (Path(job.out_root) / "results.json").write_text(json.dumps(rows), encoding="utf-8")


class TestDashboardEmpty:
    def test_empty_state_shapes(self, client) -> None:
        body = client.get("/api/dashboard").json()
        assert body["resources"] == {"skills": 0, "tasksets": 0, "jobs": 0}
        assert body["skill_health"] == []
        assert body["train_gains"] == []
        assert body["failures"] == []
        zero = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "total": 0}
        assert body["token_stats"] == {"today": zero, "total": zero}
        # pre-existing keys unchanged
        assert body["running"] == []
        assert body["recent"] == []


class TestSkillHealth:
    def test_trend_ascending_and_latest_wins(self, studio_config, client) -> None:
        early = write_job(studio_config, "eval-a1", "eval",
                          params={"skill_id": "s1"}, created_at="2026-07-01T00:00:00+00:00")
        late = write_job(studio_config, "eval-a2", "eval",
                         params={"skill_id": "s1"}, created_at="2026-07-02T00:00:00+00:00")
        write_eval_results(early, [0, 0])   # 0.0
        write_eval_results(late, [1, 1])    # 1.0

        health = client.get("/api/dashboard").json()["skill_health"]
        assert len(health) == 1
        entry = health[0]
        assert entry["skill_id"] == "s1"
        assert entry["trend"] == [0.0, 1.0]
        assert entry["last_pass_rate"] == 1.0
        assert entry["last_job_id"] == "eval-a2"
        assert entry["runs"] == 2
        assert all(0.0 <= rate <= 1.0 for rate in entry["trend"])

    def test_running_and_failed_evals_excluded(self, studio_config, client) -> None:
        done = write_job(studio_config, "eval-b1", "eval", params={"skill_id": "s1"})
        write_eval_results(done, [1])
        write_job(studio_config, "eval-b2", "eval", status="failed", params={"skill_id": "s1"})
        write_job(studio_config, "eval-b3", "eval", status="running", params={"skill_id": "s1"})

        health = client.get("/api/dashboard").json()["skill_health"]
        assert [h["runs"] for h in health] == [1]


class TestTrainGains:
    def test_gain_fields(self, studio_config, client) -> None:
        job = write_job(studio_config, "train-a1", "train", params={"skill_id": "s1"})
        summary = {
            "baseline_selection_hard": 0.4,
            "best_selection_hard": 0.65,
            "total_steps": 4, "total_accepts": 2, "total_rejects": 1, "total_skips": 1,
            "token_summary": {"_total": {"calls": 5, "prompt_tokens": 100,
                                         "completion_tokens": 50, "total_tokens": 150}},
        }
        (Path(job.out_root) / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

        gains = client.get("/api/dashboard").json()["train_gains"]
        assert len(gains) == 1
        gain = gains[0]
        assert gain["job_id"] == "train-a1"
        assert gain["skill_id"] == "s1"
        assert gain["baseline"] == 0.4
        assert gain["best"] == 0.65
        assert gain["accepts"] == 2
        assert gain["rejects"] == 1

    def test_train_without_summary_skipped(self, studio_config, client) -> None:
        write_job(studio_config, "train-b1", "train")
        assert client.get("/api/dashboard").json()["train_gains"] == []


class TestFailures:
    def test_log_tail_present_and_bounded(self, studio_config, client) -> None:
        job = write_job(studio_config, "eval-f1", "eval", status="failed")
        log_lines = [f"line {i}: " + "x" * 200 for i in range(10)]
        (studio_config.jobs_dir / job.id / "log.txt").write_text(
            "\n".join(log_lines), encoding="utf-8"
        )
        failures = client.get("/api/dashboard").json()["failures"]
        assert len(failures) == 1
        tail = failures[0]["log_tail"]
        assert tail != ""
        assert len(tail) <= 400
        assert "line 9" in tail  # the very last line survives the truncation

    def test_missing_log_gives_empty_tail(self, studio_config, client) -> None:
        write_job(studio_config, "eval-f2", "eval", status="failed")
        failures = client.get("/api/dashboard").json()["failures"]
        assert failures[0]["log_tail"] == ""


class TestTokenStats:
    def test_today_vs_total_buckets(self, studio_config, client) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        old = write_job(studio_config, "eval-t1", "eval",
                        created_at="2026-01-01T00:00:00+00:00")
        fresh = write_job(studio_config, "eval-t2", "eval",
                          created_at=f"{today}T01:00:00+00:00")
        write_eval_results(old, [1])    # per row: 180 exec + 315 judge = 495
        write_eval_results(fresh, [1])

        stats = client.get("/api/dashboard").json()["token_stats"]
        assert stats["total"]["total"] == 495 * 2
        assert stats["today"]["total"] == 495
        assert stats["today"]["input"] == 400
        assert stats["today"]["cache_write"] == 10
        assert stats["today"]["cache_read"] == 50
        assert stats["today"]["output"] == 35


class TestResources:
    def test_counts_jobs_and_skills(self, studio_config, client) -> None:
        write_job(studio_config, "echo-r1", "echo")
        skill_dir = studio_config.skill_sources["claude"] / "myskill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# skill\n", encoding="utf-8")

        resources = client.get("/api/dashboard").json()["resources"]
        assert resources["jobs"] == 1
        assert resources["skills"] == 1
        assert resources["tasksets"] == 0
