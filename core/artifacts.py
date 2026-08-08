"""In-memory artifact store for full, potentially large tool outputs."""

from __future__ import annotations

import uuid


class ArtifactStore:
    """Stores full tool outputs by artifact id.

    v1 keeps artifacts in memory for the duration of a run. Disk persistence
    is a deliberate future extension point.
    """

    def __init__(self) -> None:
        self._content: dict[str, str] = {}

    def put(self, content: str, tool: str, summary: str) -> str:
        artifact_id = "artifact_" + uuid.uuid4().hex[:8]
        while artifact_id in self._content:
            artifact_id = "artifact_" + uuid.uuid4().hex[:8]
        self._content[artifact_id] = content
        return artifact_id

    def get(self, artifact_id: str) -> str | None:
        return self._content.get(artifact_id)

    def size(self, artifact_id: str) -> int:
        content = self._content.get(artifact_id)
        return len(content) if content is not None else 0

    def ids(self) -> list[str]:
        return sorted(self._content)

