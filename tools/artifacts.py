"""Artifact reading for progressive disclosure of large tool outputs."""

from core.tool import ToolContext, clamp_int, tool
from core.types import ToolResult


@tool(cacheable=True)
def read(ctx: ToolContext, artifact_id: str, max_chars: int = 8000, offset: int = 0) -> ToolResult:
    """Read one character range from a saved tool output.

    The harness stores oversized tool outputs as artifacts instead of sending
    them to the model. The result reports the returned range, total length,
    and next_offset when more content remains. Continue from that offset.

    Args:
        artifact_id: Artifact id shown in AVAILABLE ARTIFACTS.
        max_chars: Maximum number of characters to return.
        offset: Zero-based character offset for the range.
    """
    content = ctx.artifact_store.get(artifact_id)
    if content is None:
        raise ValueError(f"No artifact with id {artifact_id!r}")
    total = len(content)
    start = clamp_int(offset, 0, total, 0)
    limit = min(ctx.max_tool_result_chars, clamp_int(max_chars, 1, 1_000_000, 8000))
    end = min(start + limit, total)
    next_offset = end if end < total else None
    return ToolResult(
        ok=True,
        summary=f"Artifact {artifact_id} characters [{start}, {end}) of {total}; next_offset={next_offset}",
        data=content[start:end],
        truncated=start > 0 or next_offset is not None,
    )
