"""API routers and shared request-scoped dependencies."""
from __future__ import annotations

from fastapi import Request

from skillopt_studio.config import StudioConfig
from skillopt_studio.jobs import JobManager


def get_config(request: Request) -> StudioConfig:
    return request.app.state.config


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.jobs
