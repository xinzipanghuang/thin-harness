"""Small local-daily tools: current time and a simple todo list."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core.tool import ToolContext, tool


@tool(name="daily.now")
def now(ctx: ToolContext) -> dict:
    """Return the current local date, time, and timezone."""
    current = datetime.now().astimezone()
    return {
        "datetime": current.isoformat(timespec="seconds"),
        "date": current.date().isoformat(),
        "time": current.strftime("%H:%M:%S"),
        "timezone": current.tzname(),
    }


@tool(name="daily.todo")
def todo(ctx: ToolContext, action: str = "list", item: str = "") -> dict:
    """Manage a simple local todo list (data/todo.json under the workdir).

    Args:
        action: "list" (default), "add", or "done".
        item: Todo text; required for add and done.
    """
    path = Path(ctx.workdir or ".") / "data" / "todo.json"
    entries: list[dict] = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
    action = (action or "list").strip().lower()
    if action == "add":
        if not item.strip():
            raise ValueError("item is required for todo add")
        entries.append({"id": len(entries) + 1, "text": item.strip(), "done": False})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"added": item.strip(), "total": len(entries)}
    if action == "done":
        if not item.strip():
            raise ValueError("item text is required for todo done")
        target = item.strip()
        matched = [entry for entry in entries if entry["text"] == target]
        if not matched:
            raise ValueError(f"No open todo matches {target!r}")
        for entry in entries:
            if entry["text"] == target:
                entry["done"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"done": target, "remaining": sum(1 for e in entries if not e["done"])}
    if action != "list":
        raise ValueError(f"Unknown action {action!r} (list|add|done)")
    return {"items": entries, "open": sum(1 for e in entries if not e["done"])}


@tool(name="daily.experiences")
async def experiences(ctx: ToolContext, limit: int = 20) -> dict:
    """List the accumulated experience records (经验库): id, problem type, keywords, method, uses.

    Experiences are JSON records the agent learns from past successful runs.
    Use this to review what the system has learned so far.

    Args:
        limit: maximum number of records to return (1-100, default 20).
    """
    # Async on purpose: sync tools run in a worker thread where peewee would
    # open a thread-local connection that never closes (locks the DB file).
    memory = ctx.memory
    if memory is None:
        return {"items": [], "total": 0, "note": "memory is not available"}
    limit = max(1, min(int(limit or 20), 100))
    items = memory.list_experiences(limit=limit)
    return {
        "items": items,
        "total": memory.count_experiences(),
        "returned": len(items),
    }


@tool(name="daily.forget")
async def forget(ctx: ToolContext, exp_id: int = 0, keyword: str = "") -> dict:
    """Delete incorrect learned experiences (经验) by numeric id or keyword.

    If the user says a past experience/method was wrong, delete it so the agent
    stops reusing it. Pass exp_id (from daily.experiences) or a keyword that
    appears in the record.

    Args:
        exp_id: numeric id of the experience record to delete.
        keyword: text to match against request/problem type/keywords/method.
    """
    # Async on purpose: see daily.experiences — keeps peewee on the loop thread.
    memory = ctx.memory
    if memory is None:
        raise ValueError("memory is not available in this context")
    deleted = memory.delete_experience(exp_id=int(exp_id or 0), keyword=keyword or "")
    return {"deleted": deleted}


@tool(name="daily.update")
async def update_experience(
    ctx: ToolContext,
    exp_id: int,
    method: str = "",
    keywords: list[str] | None = None,
    result: str = "",
    problem_type: str = "",
    success: bool | None = None,
    time_sensitive: bool | None = None,
) -> dict:
    """Update an existing experience record (经验) in place by its id.

    Use when a stored method is outdated or wrong but the task itself is worth
    keeping: e.g. "把第 3 条经验改成先查文档再动手". Only the fields you
    pass are changed; empty values are ignored (pass success=false to mark a
    method as failed without deleting it).

    Args:
        exp_id: numeric id from daily.experiences.
        method: new methodology text (optional).
        keywords: full replacement keyword list (optional).
        result: new one-line outcome (optional).
        problem_type: new problem category (optional).
        success: true/false to mark the method as working/failed (optional).
        time_sensitive: true if the method only stays valid for a short time
            (weather/news/prices), false for stable methodology (optional).
    """
    # Async on purpose: see daily.experiences — keeps peewee on the loop thread.
    memory = ctx.memory
    if memory is None:
        raise ValueError("memory is not available in this context")
    updated = memory.update_experience(
        exp_id=int(exp_id or 0),
        method=method,
        keywords=keywords,
        result=result,
        problem_type=problem_type,
        success=success,
        time_sensitive=time_sensitive,
    )
    if updated is None:
        raise ValueError(f"No experience with id {exp_id}")
    return {"updated": updated}
