"""MCP server exposing OwnTracks Recorder data, plus location reminders.

Run ``owntracks-mcp`` (stdio, the default for Hermes Agent) or
``owntracks-mcp --transport streamable-http --port 8765`` to serve over HTTP.
``owntracks-mcp check-reminders`` runs one reminder check (for a Hermes cron job)
and ``owntracks-mcp setup-reminders`` installs that cron script.
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
from pathlib import Path
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
from . import reminders as rem
from .client import RecorderClient, RecorderError
from .config import ConfigError, Settings, recorder_env
from .service import OwnTracksService

INSTRUCTIONS = """\
Access to OwnTracks location data stored in an OwnTracks Recorder.
Use get_last_location for "where is X / where am I", get_location_history for
"where was X yesterday / what route", get_distance for "how far is X from home /
from a place", list_devices to discover user and device names.
create_location_reminder sets up "remind me of X when I arrive at / leave Y"
reminders, delivered by a cron checker; list/delete manage them.
The user "me" means the configured default user. Only the reminder tools write;
everything else is read-only. Location data is personal: only share it with the
person asking.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DELETES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

mcp = _Server("owntracks", instructions=INSTRUCTIONS)

_service: OwnTracksService | None = None


def get_service() -> OwnTracksService:
    """Create the service lazily so configuration errors surface as tool errors."""
    global _service
    if _service is None:
        settings = Settings.from_env()
        store = rem.ReminderStore(rem.data_dir())
        env = recorder_env()
        if store.path.exists():  # keep the checker's copy of the settings current
            store.save_recorder_config(env)
        _service = OwnTracksService(settings, RecorderClient(settings), store=store, recorder_env=env)
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


@mcp.tool(annotations=WRITES)
@explain_errors
async def create_location_reminder(
    text: Annotated[str, Field(description="What to remind about, in the user's words, e.g. 'Buy milk'.")],
    place: Annotated[
        str,
        Field(
            description="Where: the name of a region defined in the OwnTracks app (e.g. 'Work'), "
            "'home' (uses OWNTRACKS_HOME), or a short label when lat/lon are given (e.g. 'Edeka Hammer Str.')."
        ),
    ],
    trigger: Annotated[str, Field(description="'arrive' or 'leave'.")] = "arrive",
    user: UserArg = "me",
    device: DeviceArg = None,
    lat: Annotated[float | None, Field(ge=-90, le=90, description="Latitude of the place (if not a region).")] = None,
    lon: Annotated[float | None, Field(ge=-180, le=180, description="Longitude of the place (if not a region).")] = None,
    radius_m: Annotated[float, Field(ge=25, le=50000, description="Radius around lat/lon or home.")] = 150,
    repeat: Annotated[bool, Field(description="Fire every time instead of only once.")] = False,
    expires: Annotated[
        str | None,
        Field(description="Optional end, e.g. 'today', '2026-10-01' or '3d'. Relative values count from now."),
    ] = None,
) -> dict[str, Any]:
    """Remind the user when someone arrives at or leaves a place ("remind me to buy milk when I'm at Edeka").

    For shops/addresses, look up the coordinates first and pass lat/lon plus a label.
    Reminders about other people only work for users in the same OwnTracks Recorder.
    """
    return await get_service().create_reminder(
        text, place, trigger, user, device, lat, lon, radius_m, repeat, expires
    )


@mcp.tool(annotations=READ_ONLY)
@explain_errors
async def list_location_reminders(
    include_inactive: Annotated[bool, Field(description="Also show fired and expired reminders.")] = False,
) -> dict[str, Any]:
    """List location reminders and whether the reminder checker is running."""
    return await get_service().list_reminders(include_inactive)


@mcp.tool(annotations=DELETES)
@explain_errors
async def delete_location_reminder(
    reminder_id: Annotated[str, Field(description="The id from list_location_reminders.")],
) -> dict[str, Any]:
    """Delete a location reminder."""
    return await get_service().delete_reminder(reminder_id)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every request URL

    parser = argparse.ArgumentParser(prog="owntracks-mcp", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server (default)")
    for p in (parser, serve):
        p.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
        p.add_argument("--host", default="127.0.0.1", help="Bind address for HTTP transports")
        p.add_argument("--port", type=int, default=8765, help="Port for HTTP transports")

    sub.add_parser("check-reminders", help="Run one location-reminder check (for a Hermes cron job)")

    setup = sub.add_parser("setup-reminders", help="Install the reminder cron script into Hermes")
    setup.add_argument("--hermes-home", type=Path, help="Hermes home (default: $HERMES_HOME or ~/.hermes)")
    setup.add_argument("--data-dir", type=Path, help="Where reminders are stored (default: <hermes home>/owntracks)")
    setup.add_argument("--deliver", default="telegram", help="Delivery target shown in the cron command")

    args = parser.parse_args(argv)

    if args.command == "check-reminders":
        from .checker import run_check

        sys.exit(run_check())
    if args.command == "setup-reminders":
        from .checker import setup_reminders

        sys.exit(setup_reminders(args.hermes_home, args.data_dir, args.deliver))

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
