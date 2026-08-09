"""Web search tools (DuckDuckGo, no API key required).

This module is self-contained: it does NOT import anything from
external/rag_agent/. It wraps the `ddgs` library (formerly
`duckduckgo_search`) directly.
"""

from __future__ import annotations

from core.tool import ToolContext, tool


@tool(name="web.search", cacheable=True)
def search(
    ctx: ToolContext,
    query: str,
    channel: str = "general",
    max_results: int = 10,
) -> dict:
    """Search the web with DuckDuckGo (no API key needed).

    Args:
        query: The search keywords. Use the most important words/terms
            (including synonyms) from the original request.
        channel: "general" for broad web search, or "news" for recent
            news articles.
        max_results: Maximum number of results to return (1-20).
    """
    from ddgs import DDGS

    if not query or not query.strip():
        return {"results": [], "error": "query is required"}

    max_results = max(1, min(int(max_results), 20))
    channel = (channel or "general").strip().lower()
    if channel not in ("general", "news"):
        channel = "general"

    try:
        with DDGS() as ddgs:
            if channel == "news":
                raw = ddgs.news(query, max_results=max_results)
                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", r.get("href", "")),
                        "body": r.get("body", ""),
                        "date": r.get("date", ""),
                        "source": r.get("source", ""),
                    }
                    for r in raw
                ]
            else:
                raw = ddgs.text(query, max_results=max_results)
                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", r.get("url", "")),
                        "body": r.get("body", ""),
                    }
                    for r in raw
                ]
        return {"channel": channel, "query": query, "results": results}
    except Exception as e:  # noqa: BLE001 - surface any search failure
        return {"channel": channel, "query": query, "results": [], "error": str(e)}
