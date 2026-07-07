"""Skill discovery and upload endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from skillopt_studio import skill_sources
from skillopt_studio.api import get_config
from skillopt_studio.config import StudioConfig
from skillopt_studio.models import SkillDetail, SkillInfo

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
