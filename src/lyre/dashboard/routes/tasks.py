"""Tasks tab — recent tasks + per-task detail (parent/child tree)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_list(
    request: Request, status: str | None = None
) -> HTMLResponse:
    repos = request.app.state.repos
    rows = await repos.tasks.find_recent(limit=100, status_filter=status)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "tasks.html",
        {"tab": "tasks", "tasks": rows, "filter_status": status},
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(task_id: str, request: Request) -> HTMLResponse:
    repos = request.app.state.repos
    task = await repos.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    children = await repos.tasks.find_children(task_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "task_detail.html",
        {"tab": "tasks", "task": task, "children": children},
    )
