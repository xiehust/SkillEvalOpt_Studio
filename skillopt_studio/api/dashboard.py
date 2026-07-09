"""Dashboard aggregation endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from skillopt_studio import artifacts
from skillopt_studio.api import get_config, get_job_manager
from skillopt_studio.config import StudioConfig
from skillopt_studio.jobs import JobManager
from skillopt_studio.models import JobInfo
from skillopt_studio.skill_sources import scan_skills
from skillopt_studio.tasksets import list_tasksets

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

RECENT_LIMIT = 10
EVAL_SCAN_LIMIT = 50   # newest succeeded eval jobs scanned for skill health
SKILL_HEALTH_LIMIT = 8
TREND_LIMIT = 8
TRAIN_GAINS_LIMIT = 5
FAILURES_LIMIT = 5
LOG_TAIL_LINES = 5
LOG_TAIL_MAX_CHARS = 400

_USAGE_KEYS = ("input", "cache_write", "cache_read", "output", "total")


def _zero_usage() -> dict:
    return dict.fromkeys(_USAGE_KEYS, 0)


# succeeded evals' results.json is immutable — cache pass rates by job id so
# the 3s polling loop doesn't re-read up to EVAL_SCAN_LIMIT files every tick.
_PASS_RATE_CACHE: dict[str, float | None] = {}
_PASS_RATE_CACHE_MAX = 256


def _eval_pass_rate(config: StudioConfig, job: JobInfo) -> float | None:
    if job.id in _PASS_RATE_CACHE:
        return _PASS_RATE_CACHE[job.id]
    results = artifacts.eval_results(config, job)
    rate = results["summary"]["pass_rate"] if results else None
    if len(_PASS_RATE_CACHE) >= _PASS_RATE_CACHE_MAX:
        _PASS_RATE_CACHE.clear()
    _PASS_RATE_CACHE[job.id] = rate
    return rate


def _skill_health(config: StudioConfig, all_jobs: list[JobInfo]) -> list[dict]:
    """Latest pass rate + trend per skill over recent succeeded eval jobs."""
    evals = [j for j in all_jobs if j.type == "eval" and j.status == "succeeded"][:EVAL_SCAN_LIMIT]
    by_skill: dict[str, list[JobInfo]] = {}
    for job in evals:
        skill_id = str(job.params.get("skill_id") or "")
        if skill_id:
            by_skill.setdefault(skill_id, []).append(job)

    health = []
    for skill_id, jobs_for_skill in by_skill.items():
        chronological = sorted(jobs_for_skill, key=lambda j: j.created_at)
        points = [
            (job, rate)
            for job in chronological
            if (rate := _eval_pass_rate(config, job)) is not None
        ]
        if not points:
            continue
        last_job, last_rate = points[-1]
        health.append({
            "skill_id": skill_id,
            "last_pass_rate": last_rate,
            "last_job_id": last_job.id,
            "last_run_at": last_job.created_at,
            "runs": len(points),
            "trend": [rate for _, rate in points][-TREND_LIMIT:],
        })
    health.sort(key=lambda h: h["last_run_at"], reverse=True)
    return health[:SKILL_HEALTH_LIMIT]


def _train_gains(config: StudioConfig, all_jobs: list[JobInfo]) -> list[dict]:
    gains = []
    for job in all_jobs:
        if job.type != "train" or job.status != "succeeded":
            continue
        summary = artifacts.train_summary(config, job)
        if summary is None:
            continue
        totals = summary.get("totals") or {}
        gains.append({
            "job_id": job.id,
            "skill_id": str(job.params.get("skill_id") or "") or None,
            "baseline": summary.get("baseline_selection_hard"),
            "best": summary.get("best_score"),
            "accepts": totals.get("accepts"),
            "rejects": totals.get("rejects"),
            "finished_at": job.finished_at,
        })
        if len(gains) >= TRAIN_GAINS_LIMIT:
            break
    return gains


def _log_tail(jobs: JobManager, job_id: str) -> str:
    try:
        content = str(jobs.read_log(job_id).get("content") or "")
    except (KeyError, OSError):
        return ""
    lines = [ln for ln in content.splitlines() if ln.strip()]
    return "\n".join(lines[-LOG_TAIL_LINES:])[-LOG_TAIL_MAX_CHARS:]


def _failures(jobs: JobManager, all_jobs: list[JobInfo]) -> list[dict]:
    failed = [j for j in all_jobs if j.status == "failed"][:FAILURES_LIMIT]
    return [
        {
            "job_id": job.id,
            "type": job.type,
            "skill_id": str(job.params.get("skill_id") or "") or None,
            "finished_at": job.finished_at,
            "log_tail": _log_tail(jobs, job.id),
        }
        for job in failed
    ]


def _token_stats(config: StudioConfig, all_jobs: list[JobInfo]) -> dict:
    today_key = datetime.now(timezone.utc).date().isoformat()
    stats = {"today": _zero_usage(), "total": _zero_usage()}
    for job in all_jobs:
        tokens = artifacts.job_tokens(config, job)
        if not tokens:
            continue
        buckets = ["total"]
        if str(job.created_at or "")[:10] == today_key:
            buckets.append("today")
        for bucket in buckets:
            for key in _USAGE_KEYS:
                stats[bucket][key] += int(tokens.get(key) or 0)
    return stats


@router.get("")
def dashboard(
    jobs: JobManager = Depends(get_job_manager),
    config: StudioConfig = Depends(get_config),
) -> dict:
    all_jobs = jobs.list_jobs()  # already newest-first
    totals: dict[str, int] = {}
    for job in all_jobs:
        totals[job.status] = totals.get(job.status, 0) + 1

    running = []
    for job in all_jobs:
        if job.status != "running":
            continue
        row = job.model_dump()
        row["progress"] = artifacts.job_progress(config, job)
        running.append(row)

    recent = []
    for job in all_jobs[:RECENT_LIMIT]:
        row = job.model_dump()
        row["progress"] = artifacts.job_progress(config, job)
        row["tokens"] = artifacts.job_tokens(config, job)
        if job.type == "eval" and job.status == "succeeded":
            results = artifacts.eval_results(config, job)
            row["pass_rate"] = results["summary"]["pass_rate"] if results else None
        recent.append(row)

    return {
        "running": running,
        "recent": recent,
        "totals": {"by_status": totals},
        "resources": {
            "skills": len(scan_skills(config)),
            "tasksets": len(list_tasksets(config)),
            "jobs": len(all_jobs),
        },
        "skill_health": _skill_health(config, all_jobs),
        "train_gains": _train_gains(config, all_jobs),
        "failures": _failures(jobs, all_jobs),
        "token_stats": _token_stats(config, all_jobs),
    }
