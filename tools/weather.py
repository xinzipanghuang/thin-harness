"""Weather lookup tool (wttr.in, no API key required).

This is a minimal, self-contained example of a *concrete-task* tool: given a
city name it returns the current weather. It uses the free wttr.in service
(https://wttr.in) which needs no API key and no registration — the simplest
possible path for a real-world query.

Pattern to learn/evolve from:
  - one module per concrete task (tools/<task>.py)
  - auto-discovered by the harness (no registration needed)
  - stdlib only (urllib) so there is no new dependency
  - returns a small, structured dict the model can read directly
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from core.tool import ToolContext, tool


@tool(name="daily.weather", cacheable=True)
def weather(ctx: ToolContext, city: str = "") -> dict:
    """Get the current weather for a city (no API key needed).

    Args:
        city: City name, e.g. "Hangzhou" or "杭州". If empty, uses the
            caller's approximate location.
    """
    city = (city or "").strip()
    query = urllib.parse.quote(city) if city else ""
    url = f"https://wttr.in/{query}?format=j1"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - surface any fetch failure
        return {"city": city, "error": str(e)}

    try:
        current = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0]
        area_name = area.get("areaName", [{}])[0].get("value", city or "?")
        region = area.get("region", [{}])[0].get("value", "")
        country = area.get("country", [{}])[0].get("value", "")
        return {
            "city": city or area_name,
            "location": f"{area_name}, {region}, {country}".strip(", "),
            "temp_c": current.get("temp_C"),
            "feels_like_c": current.get("FeelsLikeC"),
            "humidity": current.get("humidity"),
            "wind_kph": current.get("windspeedKmph"),
            "condition": current.get("weatherDesc", [{}])[0].get("value"),
            "observation_time": current.get("observation_time"),
        }
    except (KeyError, IndexError, TypeError) as e:  # noqa: BLE001
        return {"city": city, "error": f"unexpected response shape: {e}"}
