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
