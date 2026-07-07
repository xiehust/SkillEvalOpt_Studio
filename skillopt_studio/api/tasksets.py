"""Task set CRUD endpoints (validated skilleval task files)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from skillopt_studio import tasksets
from skillopt_studio.api import get_config
from skillopt_studio.config import StudioConfig
from skillopt_studio.models import TaskSetInfo, TaskSetItemsCreate, TaskSetUpdate

router = APIRouter(prefix="/tasksets", tags=["tasksets"])

PREVIEW_LIMIT = 20


@router.get("", response_model=list[TaskSetInfo])
def list_tasksets(config: StudioConfig = Depends(get_config)) -> list[TaskSetInfo]:
    return tasksets.list_tasksets(config)


@router.post("", response_model=TaskSetInfo)
async def create_taskset(
    name: str = Form(...),
    mode: str = Form(...),
    tasks: UploadFile | None = File(None),
    train: UploadFile | None = File(None),
    val: UploadFile | None = File(None),
    test: UploadFile | None = File(None),
    config: StudioConfig = Depends(get_config),
) -> TaskSetInfo:
    uploads = {"tasks": tasks, "train": train, "val": val, "test": test}
    files = {key: await upload.read() for key, upload in uploads.items() if upload is not None}
    try:
        return tasksets.save_taskset(config, name, files, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/items", response_model=TaskSetInfo)
def create_taskset_items(
    body: TaskSetItemsCreate, config: StudioConfig = Depends(get_config)
) -> TaskSetInfo:
    try:
        return tasksets.create_taskset_from_items(config, body.name, body.mode, body.tasks_by_split)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{taskset_id}")
def get_taskset(
    taskset_id: str,
    full: bool = Query(False),
    config: StudioConfig = Depends(get_config),
) -> dict:
    info = tasksets.get_taskset(config, taskset_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"task set {taskset_id!r} not found")
    try:
        preview = 0 if full else PREVIEW_LIMIT
        tasks_by_split = tasksets.get_taskset_tasks(config, taskset_id, preview=preview)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"info": info.model_dump(), "tasks_by_split": tasks_by_split}


@router.put("/{taskset_id}", response_model=TaskSetInfo)
def update_taskset(
    taskset_id: str, body: TaskSetUpdate, config: StudioConfig = Depends(get_config)
) -> TaskSetInfo:
    try:
        return tasksets.update_taskset(config, taskset_id, body.tasks_by_split, name=body.name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"task set {taskset_id!r} not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{taskset_id}")
def delete_taskset(taskset_id: str, config: StudioConfig = Depends(get_config)) -> dict:
    if not tasksets.delete_taskset(config, taskset_id):
        raise HTTPException(status_code=404, detail=f"task set {taskset_id!r} not found")
    return {"ok": True}
