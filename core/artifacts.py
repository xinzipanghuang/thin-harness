"""In-memory artifact store for full, potentially large tool outputs."""

from __future__ import annotations

import uuid
from typing import Callable


class ArtifactStore:
    """Stores full tool outputs by artifact id.

    A caller can supply a scoped lookup for artifacts from previous runs.
    """

    def __init__(self, load: Callable[[str], str | None] | None = None) -> None:
        self._content: dict[str, str] = {}
        self._load = load

    def put(self, content: str, tool: str, summary: str) -> str:
        artifact_id = "artifact_" + uuid.uuid4().hex[:8]
        while artifact_id in self._content:
            artifact_id = "artifact_" + uuid.uuid4().hex[:8]
        self._content[artifact_id] = content
        return artifact_id

    def get(self, artifact_id: str) -> str | None:
        if artifact_id in self._content:
            return self._content[artifact_id]
        return self._load(artifact_id) if self._load else None

    def size(self, artifact_id: str) -> int:
        content = self._content.get(artifact_id)
        return len(content) if content is not None else 0

    def ids(self) -> list[str]:
        return sorted(self._content)

