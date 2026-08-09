"""Small PDF helpers owned by the FAQ domain."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from core.tool import ToolContext, ToolResult, tool


@tool(name="faq.count_pages", cacheable=True)
def count_pages(ctx: ToolContext, path: str) -> ToolResult:
    """Count pages in a PDF and retain parser/file provenance.

    Args:
        path: PDF file path (absolute, or relative to the run workdir).
    """
    from tools.documents import _pdf_pages, _resolve

    resolved = _resolve(ctx, path)
    pages = len(_pdf_pages(resolved))
    try:
        parser_version = version("pypdf")
    except PackageNotFoundError:
        parser_version = "unknown"
    return ToolResult(
        ok=True,
        summary=f"{resolved.name}: {pages} page(s)",
        data={"path": str(resolved), "pages": pages},
        provenance={
            "source": str(resolved),
            "parser": "pypdf",
            "parser_version": parser_version,
        },
    )
