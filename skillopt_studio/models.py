"""Pydantic response/request models shared by the studio API routers."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

TaskSetMode = Literal["single", "split"]


class SkillInfo(BaseModel):
    id: str
    name: str
    source: str
    path: str
    description: str = ""
    files_count: int = 0
    has_support_files: bool = False


class SkillDetail(SkillInfo):
    skill_md: str = ""
    file_tree: list[str] = Field(default_factory=list)


class SkillFile(BaseModel):
    """One file inside a skill directory (binary files return metadata only)."""

    path: str
    kind: Literal["text", "binary"]
    size: int
    truncated: bool = False
    content: Optional[str] = None


class TaskSetInfo(BaseModel):
    id: str
    name: str
    mode: TaskSetMode
    task_count: int
    counts_by_split: dict[str, int] = Field(default_factory=dict)
    created_at: str
    updated_at: Optional[str] = None  # absent on task sets never edited
    sample: bool = False  # built-in read-only sample (materialized at startup)


class TaskSetItemsCreate(BaseModel):
    """JSON-body create (manual editor / AI-generated import path)."""

    name: str
    mode: TaskSetMode = "single"
    tasks_by_split: dict[str, list[dict]]


class TaskSetUpdate(BaseModel):
    """Full-replace update: carries ALL splits that should exist after the edit."""

    name: Optional[str] = None
    tasks_by_split: dict[str, list[dict]]


class JobInfo(BaseModel):
    id: str
    # "eval" | "train" (+ "echo", an internal harmless test type used before the
    # real runners land); kept as str so runners can extend without a schema bump.
    type: str
    status: JobStatus
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    params: dict = Field(default_factory=dict)
    out_root: Optional[str] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    # token totals filled by the API layer from artifacts.job_tokens; never
    # persisted into job.json (display aggregate, not job state).
    tokens: Optional[dict] = None


class JobCreateRequest(BaseModel):
    type: str
    params: dict = Field(default_factory=dict)


class LogChunk(BaseModel):
    content: str
    next_offset: int
