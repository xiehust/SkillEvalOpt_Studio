"""Dashboard aggregation endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from skillopt_studio import artifacts
from skillopt_studio.api import get_config, get_job_manager
from skillopt_studio.config import StudioConfig
from skillopt_studio.jobs import JobManager

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

RECENT_LIMIT = 10


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
        if job.type == "eval" and job.status == "succeeded":
            results = artifacts.eval_results(config, job)
            row["pass_rate"] = results["summary"]["pass_rate"] if results else None
        recent.append(row)

    return {"running": running, "recent": recent, "totals": {"by_status": totals}}
