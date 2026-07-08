"""Skill discovery and upload endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from skillopt_studio import skill_sources
from skillopt_studio.api import get_config
from skillopt_studio.config import StudioConfig
from skillopt_studio.models import SkillDetail, SkillFile, SkillInfo

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillInfo])
def list_skills(config: StudioConfig = Depends(get_config)) -> list[SkillInfo]:
    return skill_sources.scan_skills(config)


@router.post("/upload", response_model=SkillInfo)
async def upload_skill(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    config: StudioConfig = Depends(get_config),
) -> SkillInfo:
    data = await file.read()
    skill_name = name or Path(file.filename or "skill.zip").stem
    try:
        return skill_sources.upload_skill_zip(config, data, skill_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{skill_id}", response_model=SkillDetail)
def get_skill_detail(skill_id: str, config: StudioConfig = Depends(get_config)) -> SkillDetail:
    detail = skill_sources.get_skill_detail(config, skill_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")
    return detail


@router.get("/{skill_id}/files", response_model=SkillFile)
def get_skill_file(
    skill_id: str, path: str = "", config: StudioConfig = Depends(get_config)
) -> SkillFile:
    """In-page preview: text content as JSON (binary files → metadata only)."""
    try:
        skill_file = skill_sources.read_skill_file(config, skill_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if skill_file is None:
        raise HTTPException(status_code=404, detail=f"file {path!r} not found in skill {skill_id!r}")
    return skill_file


@router.get("/{skill_id}/files/raw")
def download_skill_file(
    skill_id: str, path: str = "", config: StudioConfig = Depends(get_config)
) -> FileResponse:
    """Raw bytes with attachment disposition — the 下载 button links here."""
    try:
        target = skill_sources.skill_file_path(config, skill_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if target is None:
        raise HTTPException(status_code=404, detail=f"file {path!r} not found in skill {skill_id!r}")
    return FileResponse(target, filename=target.name)
