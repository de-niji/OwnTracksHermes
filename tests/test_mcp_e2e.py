"""Launch the real server over stdio (as Hermes does) against a fake Recorder."""

import json
import os
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fake_recorder import FakeRecorder, serve_http

READ_TOOLS = {
    "owntracks_status",
    "list_devices",
    "get_last_location",
    "get_location_history",
    "get_distance",
    "reverse_geocode",
    "list_location_reminders",
}
WRITE_TOOLS = {"create_location_reminder", "delete_location_reminder"}


@pytest.fixture
def fake():
    return FakeRecorder()


@pytest.fixture
def recorder(fake):
    server, url = serve_http(fake, username="hermes", password="s3cret")
    yield url
    server.shutdown()


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNTRACKS_DATA_DIR", str(tmp_path / "owntracks"))
    return tmp_path / "owntracks"


def _read_only(tool) -> bool:
    a = tool.annotations
    return bool(a and (getattr(a, "read_only_hint", None) or getattr(a, "readOnlyHint", None)))


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
            assert {t.name for t in tools} == READ_TOOLS | WRITE_TOOLS
            for t in tools:
                assert _read_only(t) == (t.name in READ_TOOLS), t.name

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


async def test_reminder_roundtrip_with_cron_checker(recorder, fake, data_dir):
    """Create a reminder over MCP, then run the checker the way the Hermes cron job does."""
    import subprocess

    from fake_recorder import HOME, _rec

    async with stdio_client(_params(recorder)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            created = _payload(
                await session.call_tool(
                    "create_location_reminder", {"text": "Water the plants", "place": "home"}
                )
            )
            assert created["created"]["currently_inside"] is False  # nico is at the office
            assert any("setup-reminders" in n for n in created["notes"])

    # The cron script runs with a sanitized environment: no OWNTRACKS_URL or password.
    env = {k: v for k, v in os.environ.items() if not k.startswith("OWNTRACKS_")}
    env["OWNTRACKS_DATA_DIR"] = str(data_dir)

    def check() -> str:
        out = subprocess.run(
            [sys.executable, "-m", "owntracks_mcp.server", "check-reminders"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    assert check() == ""  # still at the office: silent tick
    fake.last[("nico", "pixel")] = _rec(int(__import__("time").time()), *HOME, "nico", "pixel")
    msg = check()
    assert msg.startswith("📍 Water the plants") and "You arrived at home" in msg
    assert check() == ""  # one-shot: fires only once
