"""Launch the real server over stdio (as Hermes does) against a fake Recorder."""

import json
import os
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fake_recorder import FakeRecorder, serve_http

EXPECTED_TOOLS = {
    "owntracks_status",
    "list_devices",
    "get_last_location",
    "get_location_history",
    "get_distance",
    "reverse_geocode",
}


@pytest.fixture
def recorder():
    server, url = serve_http(FakeRecorder(), username="hermes", password="s3cret")
    yield url
    server.shutdown()


def _params(url: str, password: str = "s3cret") -> StdioServerParameters:
    env = {
        **os.environ,
        "OWNTRACKS_URL": url,
        "OWNTRACKS_USERNAME": "hermes",
        "OWNTRACKS_PASSWORD": password,
        "OWNTRACKS_TIMEZONE": "Europe/Berlin",
        "OWNTRACKS_DEFAULT_USER": "nico",
        "OWNTRACKS_HOME": "51.9607,7.6261",
    }
    return StdioServerParameters(command=sys.executable, args=["-m", "owntracks_mcp.server"], env=env)


def _payload(result):
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured) if set(structured) == {"result"} else structured
    return json.loads(result.content[0].text)


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", None))


async def test_tools_over_stdio(recorder):
    async with stdio_client(_params(recorder)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            assert {t.name for t in tools} == EXPECTED_TOOLS
            for t in tools:
                assert t.annotations and (
                    getattr(t.annotations, "read_only_hint", None) or getattr(t.annotations, "readOnlyHint", None)
                ), t.name

            status = _payload(await session.call_tool("owntracks_status", {}))
            assert status["reachable"] and status["auth"] == "basic"
            assert "s3cret" not in json.dumps(status)

            me = _payload(await session.call_tool("get_last_location", {"user": "me"}))
            assert me["locations"][0]["device"] == "pixel"
            assert me["locations"][0]["distance_from_home_km"] > 3

            hist = _payload(
                await session.call_tool(
                    "get_location_history",
                    {"user": "nico", "start": "2026-09-28", "end": "2026-09-28", "max_points": 5},
                )
            )
            assert hist["point_count"] == 49 and len(hist["points"]) == 5
            assert len(hist["stays"]) == 2

            bad = await session.call_tool("get_location_history", {"user": "anna"})
            assert _is_error(bad)
            assert "several devices" in bad.content[0].text


async def test_wrong_password_is_reported_as_tool_error(recorder):
    async with stdio_client(_params(recorder, password="wrong")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("list_devices", {})
            assert _is_error(res)
            assert "OWNTRACKS_USERNAME" in res.content[0].text
