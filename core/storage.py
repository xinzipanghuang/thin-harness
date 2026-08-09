"""SQLite persistence for Codex-style memory (via peewee).

Six tables:
- ``Session``    one row per chat session / thread
- ``Turn``       one row per run: request, response, stop reason, counters
- ``Fact``       verified facts (session-scoped, or global when session is null)
- ``Artifact``   full tool outputs, persisted so they survive the run
- ``Experience`` reusable methodology records ("evolution"): each row stores a
  JSON document (problem_type, keywords, method, result, success) plus a
  normalized request key for upserting and a usage counter for ranking
- ``DebugEvent`` structured debug records for a run, persisted on every run
  regardless of the UI debug level

``Memory`` is the thin API: load history/facts/experiences into the context
builder, save each run, remember global facts, clear a session.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


class Experience(BaseModel):
    """One reusable experience: a JSON document plus query/ranking columns.

    ``data`` is the full JSON record the model-facing context renders, e.g.::

        {
          "request": "杭州天气",
          "problem_type": "weather_query",
          "keywords": ["杭州", "天气"],
          "method": "直接调用 wttr.in 获取天气，不再用 web.search",
          "result": "一次调用返回当前天气",
          "success": true,
          "stop_reason": "completed"
        }

    ``request`` is a normalized copy of ``data.request`` used to upsert: asking
    the same question again updates the existing record and bumps ``uses``
    instead of creating duplicates, so the store converges to the best method.
    """

    id = peewee.AutoField()
    session = peewee.ForeignKeyField(Session, backref="experiences", null=True)
    turn = peewee.ForeignKeyField(Turn, backref="experiences", null=True)
    request = peewee.TextField()
    data = peewee.TextField()
    uses = peewee.IntegerField(default=0)
    created_at = peewee.DateTimeField(default=datetime.utcnow)
    updated_at = peewee.DateTimeField(default=datetime.utcnow)


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
        self.db.bind([Session, Turn, Fact, Artifact, Experience, DebugEvent])
        self.db.connect()
        self.db.create_tables([Session, Turn, Fact, Artifact, Experience, DebugEvent])
        try:
            self.dedupe_experiences()
        except Exception:
            # Startup must never fail because of an optional cleanup pass.
            pass

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
        """Return the most recent ``limit`` turns, oldest first."""
        rows = (
            Turn.select()
            .join(Session)
            .where(Session.id == session_id)
            .order_by(Turn.seq.desc())
            .limit(max(0, int(limit)))
        )
        rows = list(rows)
        return [(turn.request, turn.response) for turn in reversed(rows)]

    def load_facts(self, session_id: Optional[str] = None) -> list[Fact]:
        if session_id:
            rows = Fact.select().where(
                (Fact.session.is_null(True)) | (Fact.session_id == session_id)
            )
        else:
            rows = Fact.select().where(Fact.session.is_null(True))
        return list(rows.order_by(Fact.id))

    def session_times(
        self, session_id: str
    ) -> Optional[tuple[datetime, datetime]]:
        """Return (created_at, updated_at) for a session, or None."""
        try:
            session = Session.get(Session.id == session_id)
        except Session.DoesNotExist:
            return None
        return session.created_at, session.updated_at

    def load_artifact(self, artifact_id: str) -> Optional[str]:
        try:
            return Artifact.get(Artifact.id == artifact_id).content
        except Artifact.DoesNotExist:
            return None

    # ---- experience memory ("evolution") --------------------------------

    def save_experience(
        self,
        data: dict[str, Any],
        *,
        session_id: str = "",
        turn_id: str = "",
    ) -> int:
        """Upsert one experience (JSON document) and return its row id.

        Matching is by, in order: (1) the exact normalized request, or (2) the
        same problem type with >=2 overlapping keywords. The matched record is
        updated in place (keywords merged, method/result replaced, ``uses``
        bumped), so wording variants converge instead of splitting into
        duplicate rows.
        """
        import json

        request = _norm_key(str(data.get("request") or ""))
        if not request:
            return 0
        session = self.get_or_create_session(session_id) if session_id else None
        turn_row = None
        if turn_id:
            try:
                turn_row = Turn.get(Turn.id == turn_id)
            except Turn.DoesNotExist:
                turn_row = None
        now = datetime.utcnow()
        payload = dict(data)
        payload["request"] = str(data.get("request") or "").strip()[:200]
        payload["updated_at"] = now.isoformat(timespec="seconds")
        now_iso = now.isoformat(timespec="seconds")
        problem = str(payload.get("problem_type") or "").strip().lower()
        new_keywords = {
            str(k).strip() for k in (payload.get("keywords") or []) if str(k).strip()
        }
        existing = self._find_merge_target(request, problem, new_keywords)
        if existing is not None:
            old = _parse_experience_json(existing.data)
            old_keywords = {str(k) for k in (old.get("keywords") or [])}
            payload["keywords"] = sorted(old_keywords | new_keywords)[:12]
            if old.get("learned_at"):
                payload["learned_at"] = old["learned_at"]  # keep the birth date
            payload["last_used_at"] = now_iso
            existing.data = json.dumps(payload, ensure_ascii=False)
            existing.uses += 1
            existing.updated_at = now
            if session is not None:
                existing.session = session
            if turn_row is not None:
                existing.turn = turn_row
            existing.save()
            return existing.id
        payload.setdefault("learned_at", now_iso)
        payload.setdefault("last_used_at", now_iso)
        payload["keywords"] = sorted(new_keywords)[:12]
        row = Experience.create(
            session=session,
            turn=turn_row,
            request=request,
            data=json.dumps(payload, ensure_ascii=False),
            uses=1,
        )
        return row.id

    def _find_merge_target(
        self,
        request: str,
        problem: str,
        new_keywords: set[str],
    ) -> Optional[Experience]:
        """Find the experience row a new record should merge into.

        Priority: exact normalized request, then same problem type with >=2
        overlapping keywords. Returns None when no clear match exists, so the
        caller creates a fresh record instead of blurring distinct tasks.
        """
        rows = list(Experience.select().order_by(Experience.updated_at.desc()))
        for row in rows:
            if request and _norm_key(row.request) == request:
                return row
        if problem:
            for row in rows:
                data = _parse_experience_json(row.data)
                if str(data.get("problem_type") or "").strip().lower() != problem:
                    continue
                old_keywords = {str(k) for k in (data.get("keywords") or [])}
                if len(old_keywords & new_keywords) >= 2:
                    return row
        return None

    def search_experiences(
        self,
        request: str,
        limit: int = 3,
        include_failures: bool = False,
        stale_after_days: int = 7,
    ) -> list[dict[str, Any]]:
        """Rank stored experiences against a new request (lexical, CJK-aware).

        Matching is deliberately dependency-free: keyword substring hits,
        problem_type hits, token overlap on ASCII words / CJK bigrams. Bumps
        ``uses`` on retrieval so frequently useful records rank higher later.

        Exploration guard: an experience is only surfaced when it clearly
        applies — the problem type matches or at least two keywords appear in
        the request. Weak token overlaps alone are ignored, so the agent keeps
        exploring instead of blindly reusing a loosely-related method. Failed
        experiences (``success: false``) are excluded unless explicitly asked
        for. Time-sensitive experiences that have not been used for more than
        ``stale_after_days`` days are treated as stale and not injected —
        re-exploring refreshes them instead.
        """
        import json

        rows = list(Experience.select().order_by(Experience.updated_at.desc()))
        text = (request or "").lower()
        tokens = _tokenize(text)
        now = datetime.utcnow()

        def keywords(data: dict[str, Any]) -> list[str]:
            return [str(k).lower() for k in (data.get("keywords") or [])]

        scored: list[tuple[int, int, int, datetime, Experience]] = []
        for row in rows:
            data = _parse_experience_json(row.data)
            if not isinstance(data, dict):
                continue
            if data.get("success") is False and not include_failures:
                continue
            time_sensitive = bool(data.get("time_sensitive"))
            age_days = _days_since(
                str(data.get("last_used_at") or data.get("learned_at") or ""), now
            )
            if time_sensitive and age_days is not None and age_days > stale_after_days:
                continue
            problem = str(data.get("problem_type") or "").lower()
            problem_hit = bool(problem) and problem in text
            kw_list = keywords(data)
            keyword_hits = [kw for kw in kw_list if kw and kw in text]
            weak = sum(
                1
                for kw in kw_list
                if kw
                and kw not in text
                and (any(t in kw for t in tokens) or any(kw in t for t in tokens))
            )
            if not (problem_hit or len(keyword_hits) >= 2):
                continue
            score = (4 if problem_hit else 0) + 3 * len(keyword_hits) + weak
            if score > 0:
                scored.append(
                    (
                        score,
                        -age_days if age_days is not None else 9999,
                        row.uses,
                        row.updated_at,
                        row,
                    )
                )
        scored.sort(key=lambda item: item[:4], reverse=True)

        results: list[dict[str, Any]] = []
        for score, neg_age, _uses, _when, row in scored[: max(0, int(limit))]:
            data = _parse_experience_json(row.data)
            if not isinstance(data, dict):
                continue
            age_days = -neg_age if neg_age != 9999 else None
            out = dict(data)
            out["id"] = row.id
            out["uses"] = row.uses
            out["age_days"] = age_days
            out["stale"] = (
                bool(data.get("time_sensitive"))
                and age_days is not None
                and age_days > stale_after_days
            )
            out["_score"] = score
            results.append(out)
            row.uses += 1
            data["last_used_at"] = now.isoformat(timespec="seconds")
            row.data = json.dumps(data, ensure_ascii=False)
            row.save()
        return results

    def list_experiences(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return stored experiences (newest first) as plain dicts."""
        import json

        rows = list(
            Experience.select()
            .order_by(Experience.updated_at.desc())
            .limit(max(1, int(limit)))
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row.data) if row.data else {}
            except json.JSONDecodeError:
                data = {}
            if isinstance(data, dict):
                data["id"] = row.id
                data["uses"] = row.uses
                data["age_days"] = _days_since(
                    str(data.get("last_used_at") or data.get("learned_at") or ""),
                    datetime.utcnow(),
                )
                data["stale"] = (
                    bool(data.get("time_sensitive"))
                    and data["age_days"] is not None
                    and data["age_days"] > 7
                )
                data["created_at"] = row.created_at.isoformat()
                data["updated_at"] = row.updated_at.isoformat()
                results.append(data)
        return results

    def delete_experience(self, exp_id: int = 0, keyword: str = "") -> int:
        """Delete experiences by numeric id or by keyword match.

        Keyword matching covers request text, problem type, keywords and method
        (case-insensitive substring). Returns the number of deleted rows.
        """
        import json

        if exp_id:
            try:
                target = int(exp_id)
            except (TypeError, ValueError):
                return 0
            return Experience.delete().where(Experience.id == target).execute()
        kw = (keyword or "").strip()
        if not kw:
            return 0
        matched: list[int] = []
        for row in Experience.select():
            try:
                data = json.loads(row.data) if row.data else {}
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                continue
            haystack = " ".join(
                [
                    str(data.get("request") or ""),
                    str(data.get("problem_type") or ""),
                    " ".join(str(k) for k in (data.get("keywords") or [])),
                    str(data.get("method") or ""),
                    str(data.get("result") or ""),
                ]
            ).lower()
            if kw.lower() in haystack:
                matched.append(row.id)
        if not matched:
            return 0
        return Experience.delete().where(Experience.id.in_(matched)).execute()

    def count_experiences(self) -> int:
        return Experience.select().count()

    def update_experience(
        self,
        exp_id: int,
        *,
        method: str = "",
        keywords: Optional[list[str]] = None,
        result: str = "",
        problem_type: str = "",
        success: Optional[bool] = None,
        time_sensitive: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        """Edit one stored experience in place; returns the updated dict.

        Only the provided fields are changed; empty strings / None are left
        untouched. Returns None when the id does not exist.
        """
        import json

        try:
            row = Experience.get(Experience.id == int(exp_id))
        except (Experience.DoesNotExist, TypeError, ValueError):
            return None
        data = _parse_experience_json(row.data)
        if method:
            data["method"] = " ".join(str(method).split())[:400]
        if keywords is not None:
            data["keywords"] = [
                str(k).strip()[:30] for k in keywords if str(k).strip()
            ][:12]
        if result:
            data["result"] = " ".join(str(result).split())[:200]
        if problem_type:
            data["problem_type"] = str(problem_type).strip()[:80]
        if success is not None:
            data["success"] = bool(success)
        if time_sensitive is not None:
            data["time_sensitive"] = bool(time_sensitive)
        now = datetime.utcnow()
        data["updated_at"] = now.isoformat(timespec="seconds")
        row.data = json.dumps(data, ensure_ascii=False)
        row.request = _norm_key(str(data.get("request") or ""))
        row.updated_at = now
        row.save()
        updated = dict(data)
        updated["id"] = row.id
        updated["uses"] = row.uses
        return updated

    def dedupe_experiences(self) -> int:
        """Merge near-duplicate experience rows; returns rows removed.

        Two rows are duplicates when they share the exact normalized request,
        or the same problem type with >=2 overlapping keywords. The newest
        record wins; its keywords are merged and ``uses`` are summed. Safe to
        run repeatedly (idempotent).
        """
        import json

        rows = list(Experience.select().order_by(Experience.updated_at.desc()))
        kept: list[Experience] = []
        removed = 0
        for row in rows:
            data = _parse_experience_json(row.data)
            request = _norm_key(str(data.get("request") or ""))
            problem = str(data.get("problem_type") or "").strip().lower()
            keywords = {str(k) for k in (data.get("keywords") or [])}
            target = None
            for candidate in kept:
                cand = _parse_experience_json(candidate.data)
                cand_request = _norm_key(str(cand.get("request") or ""))
                cand_problem = str(cand.get("problem_type") or "").strip().lower()
                cand_keywords = {str(k) for k in (cand.get("keywords") or [])}
                if request and cand_request == request:
                    target = candidate
                    break
                if (
                    problem
                    and cand_problem == problem
                    and len(cand_keywords & keywords) >= 2
                ):
                    target = candidate
                    break
            if target is None:
                kept.append(row)
                continue
            target_data = _parse_experience_json(target.data)
            target_keywords = {str(k) for k in (target_data.get("keywords") or [])}
            target_data["keywords"] = sorted(target_keywords | keywords)[:12]
            target_data["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
            target.data = json.dumps(target_data, ensure_ascii=False)
            target.uses += row.uses
            target.updated_at = datetime.utcnow()
            target.save()
            row.delete_instance()
            removed += 1
        return removed

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
        Experience.delete().where(Experience.session_id == session_id).execute()
        DebugEvent.delete().where(
            DebugEvent.turn.in_(
                Turn.select(Turn.id).where(Turn.session_id == session_id)
            )
        ).execute()
        Turn.delete().where(Turn.session_id == session_id).execute()
        Fact.delete().where(Fact.session_id == session_id).execute()
        Artifact.delete().where(Artifact.session_id == session_id).execute()
        Session.delete().where(Session.id == session_id).execute()


def _norm_key(text: str) -> str:
    """Normalize a request into a stable dedupe key."""
    return " ".join(str(text or "").strip().lower().split())


def _parse_experience_json(text: str) -> dict[str, Any]:
    """Parse an Experience.data JSON blob; never raises."""
    import json

    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _days_since(iso_text: str, now: datetime) -> Optional[int]:
    """Whole days between ``iso_text`` (UTC) and ``now``; None if unparsable."""
    if not iso_text:
        return None
    try:
        value = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    try:
        delta = now - value
    except TypeError:
        # naive/aware mismatch: normalize to UTC before subtracting
        if value.tzinfo is not None and now.tzinfo is None:
            delta = now.replace(tzinfo=timezone.utc) - value
        elif value.tzinfo is None and now.tzinfo is not None:
            delta = now - value.replace(tzinfo=timezone.utc)
        else:
            return None
    return max(0, delta.days)


def _tokenize(text: str) -> set[str]:
    """ASCII words + CJK chars/bigrams; used for lexical experience matching."""
    import re

    tokens = set(re.findall(r"[a-z0-9_]+", text))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.update(cjk)
    tokens.update("".join(pair) for pair in zip(cjk, cjk[1:]))
    return tokens


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
