"""Runs tab — merged Tasks + Wakeups view with two sub-tabs.

Both are "things that ran or are running"; the design folds them under one
nav slot with a chip switcher. Tasks default. Per-row drill-down to
`/tasks/<id>` is preserved for the task-detail page.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


_TASK_STATUSES = (
    "all", "in_progress", "pending", "needs_input", "completed", "failed",
)
_WAKEUP_STATUSES = (
    "all", "running", "silent_close", "failed", "completed",
)


@router.get("/runs", response_class=HTMLResponse)
async def runs_view(
    request: Request,
    tab: str = "tasks",
    status: str = "all",
) -> HTMLResponse:
    repos = request.app.state.repos
    mcw = getattr(request.app.state, "model_context_windows", None)
    _ = mcw  # piped through to the template via context_peak_pct filter

    if tab not in ("tasks", "wakeups"):
        tab = "tasks"

    all_tasks = await repos.tasks.find_recent(limit=200)
    task_counts = {k: 0 for k in _TASK_STATUSES}
    task_counts["all"] = len(all_tasks)
    for t in all_tasks:
        if t.status in task_counts:
            task_counts[t.status] += 1

    all_wakeups = await repos.wakeups.list_recent(limit=200)
    wakeup_counts = {k: 0 for k in _WAKEUP_STATUSES}
    wakeup_counts["all"] = len(all_wakeups)
    for w in all_wakeups:
        st = w.end_status or "running"
        if st == "error":
            wakeup_counts["failed"] += 1
        elif st in wakeup_counts:
            wakeup_counts[st] += 1

    # Filter the requested tab's rows
    if tab == "tasks":
        tasks = (
            all_tasks if status == "all"
            else [t for t in all_tasks if t.status == status]
        )
        wakeups = []
    else:
        tasks = []
        if status == "all":
            wakeups = all_wakeups
        elif status == "running":
            wakeups = [w for w in all_wakeups if w.end_status is None]
        elif status == "failed":
            wakeups = [w for w in all_wakeups if w.end_status in ("failed", "error")]
        else:
            wakeups = [w for w in all_wakeups if w.end_status == status]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "runs.html",
        {
            "tab": "runs",
            "tab_kind": tab,
            "task_filter": status if tab == "tasks" else "all",
            "wakeup_filter": status if tab == "wakeups" else "all",
            "tasks": tasks,
            "wakeups": wakeups,
            "task_counts": task_counts,
            "wakeup_counts": wakeup_counts,
            "model_context_windows": mcw or {},
            "wakeups_running_count": sum(
                1 for w in all_wakeups if w.end_status is None
            ),
            # base.html header pill
            "needs_input_count": task_counts.get("needs_input", 0),
        },
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
        {"tab": "runs", "task": task, "children": children},
    )
