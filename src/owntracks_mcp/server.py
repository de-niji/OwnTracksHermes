"""MCP server exposing OwnTracks Recorder data as read-only tools.

Run ``owntracks-mcp`` (stdio, the default for Hermes Agent) or
``owntracks-mcp --transport streamable-http --port 8765`` to serve over HTTP.
"""

from __future__ import annotations

import argparse
import functools
import logging
from typing import Annotated, Any, Awaitable, Callable, TypeVar

from pydantic import Field

try:  # MCP Python SDK >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError

    _SDK_V2 = True
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

    _SDK_V2 = False

from mcp.types import ToolAnnotations

from . import __version__
from .client import RecorderClient, RecorderError
from .config import ConfigError, Settings
from .service import OwnTracksService

INSTRUCTIONS = """\
Access to OwnTracks location data stored in an OwnTracks Recorder.
Use get_last_location for "where is X / where am I", get_location_history for
"where was X yesterday / what route", get_distance for "how far is X from home /
from a place", list_devices to discover user and device names.
The user "me" means the configured default user. All tools are read-only.
Location data is personal: only share it with the person asking.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

mcp = _Server("owntracks", instructions=INSTRUCTIONS)

_service: OwnTracksService | None = None


def get_service() -> OwnTracksService:
    """Create the service lazily so configuration errors surface as tool errors."""
    global _service
    if _service is None:
        settings = Settings.from_env()
        _service = OwnTracksService(settings, RecorderClient(settings))
    return _service


T = TypeVar("T")


def explain_errors(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Pass expected failures to the agent as readable tool errors.

    The MCP SDK hides the message of unexpected exceptions; these ones are
    written for the agent (wrong user name, Recorder unreachable, ...).
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except (ConfigError, RecorderError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


UserArg = Annotated[
    str | None,
    Field(description='OwnTracks user name. "me" = configured default user. Omit for everyone (where supported).'),
]
DeviceArg = Annotated[
    str | None,
    Field(description="Device name of that user. Omit if the user has only one device."),
]


@mcp.tool(annotations=READ_ONLY)
@explain_errors
async def owntracks_status() -> dict[str, Any]:
    """Check the connection to the OwnTracks Recorder and show the active configuration (no secrets)."""
    return await get_service().status()


@mcp.tool(annotations=READ_ONLY)
@explain_errors
async def list_devices() -> dict[str, Any]:
    """List all OwnTracks users and their devices known to the Recorder."""
    return await get_service().list_devices()


@mcp.tool(annotations=READ_ONLY)
@explain_errors
async def get_last_location(user: UserArg = None, device: DeviceArg = None) -> dict[str, Any]:
    """Current (most recent) position of a user/device, or of everyone if no user is given.

    Returns coordinates, time and age of the fix, address (if the Recorder
    geocodes), regions the device is in, accuracy, speed, battery and a map link.
    Always mention how old the position is.
    """
    return await get_service().last_location(user, device)


@mcp.tool(annotations=READ_ONLY)
@explain_errors
async def get_location_history(
    user: UserArg = None,
    device: DeviceArg = None,
    start: Annotated[
        str | None,
        Field(
            description="Start of range: 'today', 'yesterday', relative like '6h'/'2d'/'1w' (ago), "
            "a date '2026-09-27' or ISO time '2026-09-27T08:00'. Default: 24h ago."
        ),
    ] = None,
    end: Annotated[
        str | None,
        Field(description="End of range, same formats as start. A plain date means end of that day. Default: now."),
    ] = None,
    max_points: Annotated[int, Field(ge=0, le=1000, description="Max track points to return (thinned evenly).")] = 50,
    stay_min_minutes: Annotated[
        float, Field(ge=1, description="Minimum time at one place to count as a stay.")
    ] = 10,
) -> dict[str, Any]:
    """Route/track of a device in a time range, with distance travelled and places where it stayed.

    Use the 'stays' list to answer "where was X" questions and 'points' for the route.
    Times are in the configured local time zone.
    """
    return await get_service().history(
        user, device, start, end, max_points=max_points, stay_min_minutes=stay_min_minutes
    )


@mcp.tool(annotations=READ_ONLY)
@explain_errors
async def get_distance(
    lat: Annotated[float | None, Field(ge=-90, le=90, description="Target latitude. Omit both to use home.")] = None,
    lon: Annotated[float | None, Field(ge=-180, le=180, description="Target longitude. Omit both to use home.")] = None,
    user: UserArg = None,
    device: DeviceArg = None,
) -> dict[str, Any]:
    """Straight-line distance from the latest position(s) to a point (or to the configured home)."""
    return await get_service().distance(lat, lon, user, device)


@mcp.tool(annotations=READ_ONLY)
@explain_errors
async def reverse_geocode(
    lat: Annotated[float, Field(ge=-90, le=90)],
    lon: Annotated[float, Field(ge=-180, le=180)],
) -> dict[str, Any]:
    """Look up an address for coordinates in the Recorder's geocache (only already-seen places)."""
    return await get_service().reverse_geocode(lat, lon)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="owntracks-mcp", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for HTTP transports")
    parser.add_argument("--port", type=int, default=8765, help="Port for HTTP transports")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every request URL

    if args.transport == "stdio":
        mcp.run("stdio")
    elif _SDK_V2:
        mcp.run(args.transport, host=args.host, port=args.port)
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(args.transport)


if __name__ == "__main__":
    main()
