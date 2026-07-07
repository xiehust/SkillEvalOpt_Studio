"""Job endpoints: submit (echo/eval/train/taskgen), list, inspect, cancel,
logs, results and artifact browsing.

``echo`` is an internal harmless job type kept for tests; ``eval``, ``train``
and ``taskgen`` build real CLI commands via the runners module.
"""
from __future__ import annotations

import shutil
import sys

from fastapi import APIRouter, Depends, HTTPException, Query

from skillopt_studio import artifacts, runners
from skillopt_studio.api import get_config, get_job_manager
from skillopt_studio.config import StudioConfig
from skillopt_studio.jobs import JobManager
from skillopt_studio.models import JobCreateRequest, JobInfo, LogChunk

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _build_echo_command(params: dict) -> list[str]:
    """Harmless internal test job; ticks>0 makes it long-running with a
    growing log so log polling and cancellation can be exercised for real."""
    message = str(params.get("message", "studio echo"))
    ticks = int(params.get("ticks", 0) or 0)
    if ticks > 0:
        code = (
            "import time\n"
            f"print({message!r}, flush=True)\n"
            f"for i in range({ticks}):\n"
            "    print(f'tick {i+1}', flush=True)\n"
            "    time.sleep(1)\n"
        )
        return [sys.executable, "-c", code]
    return [sys.executable, "-c", f"print({message!r})"]


RUNNER_TYPES = ("eval", "train", "taskgen")

_COMMAND_BUILDERS = {
    "eval": runners.build_eval_command,
    "train": runners.build_train_command,
    "taskgen": runners.build_taskgen_command,
}


@router.post("", response_model=JobInfo)
def create_job(
    body: JobCreateRequest,
    jobs: JobManager = Depends(get_job_manager),
    config: StudioConfig = Depends(get_config),
) -> JobInfo:
    if body.type == "echo":
        return jobs.create_job("echo", body.params, _build_echo_command(body.params))
    if body.type not in RUNNER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported job type {body.type!r}; supported: "
                   f"{['echo', *RUNNER_TYPES]}",
        )

    job_id = jobs.allocate_job_id(body.type)
    job_dir = config.jobs_dir / job_id
    try:
        cmd = _COMMAND_BUILDERS[body.type](config, body.params, job_dir)
    except ValueError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return jobs.create_job(
        body.type,
        body.params,
        cmd,
        cwd=str(runners.PROJECT_ROOT),
        out_root=str(job_dir / "out"),
        job_id=job_id,
    )


@router.get("", response_model=list[JobInfo])
def list_jobs(jobs: JobManager = Depends(get_job_manager)) -> list[JobInfo]:
    return jobs.list_jobs()


@router.get("/{job_id}", response_model=JobInfo)
def get_job(job_id: str, jobs: JobManager = Depends(get_job_manager)) -> JobInfo:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return job


@router.post("/{job_id}/cancel", response_model=JobInfo)
def cancel_job(job_id: str, jobs: JobManager = Depends(get_job_manager)) -> JobInfo:
    try:
        return jobs.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{job_id}/log", response_model=LogChunk)
def read_job_log(
    job_id: str,
    offset: int = Query(0, ge=0),
    jobs: JobManager = Depends(get_job_manager),
) -> LogChunk:
    try:
        return LogChunk(**jobs.read_log(job_id, offset))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc


@router.get("/{job_id}/results")
def get_job_results(
    job_id: str,
    jobs: JobManager = Depends(get_job_manager),
    config: StudioConfig = Depends(get_config),
) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    if job.type == "eval":
        results = artifacts.eval_results(config, job)
        if results is None:
            raise HTTPException(
                status_code=404,
                detail=f"results not available yet (job status: {job.status})",
            )
        return {"type": "eval", **results}
    if job.type == "train":
        summary = artifacts.train_summary(config, job)
        if summary is None:
            raise HTTPException(
                status_code=404,
                detail=f"results not available yet (job status: {job.status})",
            )
        return {"type": "train", "summary": summary, "skill_diff": artifacts.skill_diff(config, job)}
    if job.type == "taskgen":
        results = artifacts.taskgen_results(config, job)
        if results is None:
            raise HTTPException(
                status_code=404,
                detail=f"results not available yet (job status: {job.status})",
            )
        return {"type": "taskgen", **results}
    raise HTTPException(status_code=400, detail=f"job type {job.type!r} has no results view")


@router.get("/{job_id}/artifacts")
def browse_artifacts(
    job_id: str,
    path: str = Query(""),
    jobs: JobManager = Depends(get_job_manager),
    config: StudioConfig = Depends(get_config),
) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    try:
        target = artifacts.safe_target(config, job, path)
        if target.is_file():
            return artifacts.read_artifact(config, job, path)  # kind: text | binary
        return {"kind": "dir", **artifacts.list_artifacts(config, job, path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"artifact not found: {exc}") from exc
