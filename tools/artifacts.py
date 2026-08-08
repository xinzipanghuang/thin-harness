"""Artifact reading for progressive disclosure of large tool outputs."""

from core.tool import ToolContext, clamp_int, tool


@tool(cacheable=True)
def read(ctx: ToolContext, artifact_id: str, max_chars: int = 8000) -> str:
    """Read the saved content of a large tool output (an artifact).

    The harness stores oversized tool outputs as artifacts instead of sending
    them to the model. Use this tool to inspect more of that output.

    Args:
        artifact_id: Artifact id shown in AVAILABLE ARTIFACTS.
        max_chars: Maximum number of characters to return.
    """
    content = ctx.artifact_store.get(artifact_id)
    if content is None:
        raise ValueError(f"No artifact with id {artifact_id!r}")
    max_chars = clamp_int(max_chars, 1, 1_000_000, 8000)
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n…[truncated: {len(content)} total chars]"
    return content
