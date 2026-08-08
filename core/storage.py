"""SQLite persistence for Codex-style memory (via peewee).

Five tables:
- ``Session``   one row per chat session / thread
- ``Turn``      one row per run: request, response, stop reason, counters
- ``Fact``      verified facts (session-scoped, or global when session is null)
- ``Artifact``  full tool outputs, persisted so they survive the run
- ``DebugEvent`` structured debug records for a run, persisted on every run
  regardless of the UI debug level

``Memory`` is the thin API: load history/facts into the context builder, save
each run, remember global facts, clear a session.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import peewee

from .types import Artifact, Fact, RunState


class BaseModel(peewee.Model):
    class Meta:
        database = None


class Session(BaseModel):
    id = peewee.CharField(primary_key=True)
    created_at = peewee.DateTimeField(default=datetime.utcnow)
    updated_at = peewee.DateTimeField(default=datetime.utcnow)


class Turn(BaseModel):
    seq = peewee.AutoField()
    id = peewee.CharField(unique=True)  # run id
    session = peewee.ForeignKeyField(Session, backref="turns")
    request = peewee.TextField()
    response = peewee.TextField()
    stop_reason = peewee.CharField(default="completed")
    steps = peewee.IntegerField(default=0)
    tool_calls = peewee.IntegerField(default=0)
    failures = peewee.IntegerField(default=0)
    created_at = peewee.DateTimeField(default=datetime.utcnow)


class Fact(BaseModel):
    id = peewee.AutoField()
    session = peewee.ForeignKeyField(Session, backref="facts", null=True)
    value = peewee.TextField()
    source = peewee.CharField(null=True)
    tool = peewee.CharField(null=True)
    created_at = peewee.DateTimeField(default=datetime.utcnow)


class Artifact(BaseModel):
    id = peewee.CharField(primary_key=True)
    session = peewee.ForeignKeyField(Session, backref="artifacts", null=True)
    tool = peewee.CharField()
    summary = peewee.TextField()
    content = peewee.TextField()
    created_at = peewee.DateTimeField(default=datetime.utcnow)


class DebugEvent(BaseModel):
    seq = peewee.AutoField()
    turn = peewee.ForeignKeyField(Turn, backref="debug_events")
    level = peewee.IntegerField(default=0)
    kind = peewee.CharField()
    detail = peewee.TextField()
    created_at = peewee.DateTimeField(default=datetime.utcnow)


class Memory:
    """Codex-style memory over SQLite: sessions, turns, facts, artifacts."""

    def __init__(self, db_path: str = "data/agent.db") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = peewee.SqliteDatabase(str(path), pragmas={"journal_mode": "WAL"})
        self.db.bind([Session, Turn, Fact, Artifact, DebugEvent])
        self.db.connect()
        self.db.create_tables([Session, Turn, Fact, Artifact, DebugEvent])

    def close(self) -> None:
        if not self.db.is_closed():
            self.db.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- session helpers -------------------------------------------------

    def get_or_create_session(self, session_id: str) -> Session:
        session, _ = Session.get_or_create(id=session_id)
        Session.update(updated_at=datetime.utcnow()).where(Session.id == session_id).execute()
        return session

    # ---- save ------------------------------------------------------------

    def save_run(
        self,
        *,
        session_id: str,
        run_id: str,
        request: str,
        response: str,
        stop_reason: str,
        steps: int,
        tool_calls: int,
        failures: int,
        facts: list[Fact],
        artifacts: list[Artifact],
        artifact_store: Any,
        debug_events: list[Any] | None = None,
    ) -> None:
        """Persist one run: turn, new facts, and artifact contents."""
        session = self.get_or_create_session(session_id)
        turn_row = Turn.create(
            id=run_id,
            session=session,
            request=request,
            response=response,
            stop_reason=stop_reason,
            steps=steps,
            tool_calls=tool_calls,
            failures=failures,
        )
        for fact in facts:
            Fact.create(
                session=session,
                value=_fact_value(fact.value),
                source=fact.source,
                tool=fact.tool,
            )
        for artifact in artifacts:
            content = artifact_store.get(artifact.id)
            Artifact.replace(
                id=artifact.id,
                session=session,
                tool=artifact.tool,
                summary=artifact.summary,
                content=content or "",
            ).execute()
        for event in debug_events or []:
            DebugEvent.create(
                turn=turn_row,
                level=_event_level(event),
                kind=_event_kind(event),
                detail=_event_detail(event),
                created_at=_event_created_at(event),
            )

    def remember(self, value: Any, source: Optional[str] = None, tool: Optional[str] = None) -> None:
        """Persist a global (cross-session) verified fact."""
        Fact.create(session=None, value=_fact_value(value), source=source, tool=tool)

    # ---- load ------------------------------------------------------------

    def load_history(self, session_id: str, limit: int = 10) -> list[tuple[str, str]]:
        rows = (
            Turn.select()
            .join(Session)
            .where(Session.id == session_id)
            .order_by(Turn.seq)
            .limit(max(0, int(limit)))
        )
        return [(turn.request, turn.response) for turn in rows]

    def load_facts(self, session_id: Optional[str] = None) -> list[Fact]:
        if session_id:
            rows = Fact.select().where(
                (Fact.session.is_null(True)) | (Fact.session_id == session_id)
            )
        else:
            rows = Fact.select().where(Fact.session.is_null(True))
        return list(rows.order_by(Fact.id))

    def load_artifact(self, artifact_id: str) -> Optional[str]:
        try:
            return Artifact.get(Artifact.id == artifact_id).content
        except Artifact.DoesNotExist:
            return None

    def load_debug(self, run_id: str) -> list[dict[str, Any]]:
        """Return structured debug records for one run, oldest first."""
        import json

        rows = (
            DebugEvent.select()
            .join(Turn)
            .where(Turn.id == run_id)
            .order_by(DebugEvent.seq)
        )
        return [
            {
                "level": row.level,
                "kind": row.kind,
                "detail": json.loads(row.detail) if row.detail else {},
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    # ---- clear -----------------------------------------------------------

    def clear_session(self, session_id: str) -> None:
        DebugEvent.delete().where(
            DebugEvent.turn.in_(
                Turn.select(Turn.id).where(Turn.session_id == session_id)
            )
        ).execute()
        Turn.delete().where(Turn.session_id == session_id).execute()
        Fact.delete().where(Fact.session_id == session_id).execute()
        Artifact.delete().where(Artifact.session_id == session_id).execute()
        Session.delete().where(Session.id == session_id).execute()


def _fact_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _event_level(event: Any) -> int:
    if hasattr(event, "level"):
        return int(event.level or 0)
    return int((event or {}).get("level", 0) or 0)


def _event_kind(event: Any) -> str:
    if hasattr(event, "kind"):
        return str(event.kind or "")
    return str((event or {}).get("kind", "") or "")


def _event_detail(event: Any) -> str:
    if hasattr(event, "detail"):
        detail = event.detail
    else:
        detail = (event or {}).get("detail", {})
    if isinstance(detail, str):
        return detail
    import json

    try:
        return json.dumps(detail, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(detail)


def _event_created_at(event: Any) -> datetime:
    raw = getattr(event, "created_at", None) if hasattr(event, "created_at") else None
    if raw:
        try:
            return datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            pass
    return datetime.utcnow()
