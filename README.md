# OwnTracks for Hermes Agent

Connects [OwnTracks](https://owntracks.org) to [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(Nous Research), so Hermes can answer questions like:

- "Where am I right now?" / "Where is Anna?"
- "Where was I yesterday afternoon?" / "When did I get to the office today?"
- "How far is Anna from home?"
- "How many kilometres did I drive this week?"

The project has two parts:

1. **`owntracks-mcp`**: an MCP server (Python) that queries the REST API of the
   [OwnTracks Recorder](https://github.com/owntracks/recorder). It works with Hermes and
   with any other MCP client (Claude Desktop, Cursor, …).
2. **`skills/owntracks/SKILL.md`**: a Hermes skill that tells the agent when and how to use
   the tools (always state how old a position is, summarise stays, respect privacy).

All access is **read-only**. The Recorder's delete endpoint (`/api/0/kill`) is never called.

```
OwnTracks app ──MQTT/HTTP──▶ OwnTracks Recorder ◀──HTTP── owntracks-mcp ◀──MCP (stdio)── Hermes Agent
```

## Tools

| Tool (in Hermes: `mcp_owntracks_…`) | Purpose |
|---|---|
| `get_last_location(user?, device?)` | Latest position: address, regions, age, accuracy, speed, battery, map link. No `user` → everyone. `user="me"` → default user. |
| `get_location_history(user, device?, start?, end?, max_points?, stay_min_minutes?)` | History for a time range: distance travelled, top speed, **stays** (where and how long) and a thinned-out route. |
| `get_distance(lat?, lon?, user?, device?)` | Straight-line distance from the latest position to a point, or to `OWNTRACKS_HOME` when no coordinates are given. |
| `list_devices()` | All users and devices. |
| `reverse_geocode(lat, lon)` | Address from the Recorder's geocache. |
| `owntracks_status()` | Connection check and active configuration (no secrets). |

Time arguments accept `now`, `today`, `yesterday`, relative values like `30m`, `6h`, `2d`,
`1w`, as well as `2026-09-27` or `2026-09-27T14:30` (local time zone).

## Requirements

- A running **OwnTracks Recorder** that the machine running Hermes can reach over HTTP
  (default port `8083`). If you don't have one yet,
  [owntracks/quicksetup](https://github.com/owntracks/quicksetup) is the quickest route,
  or `docker run -p 8083:8083 owntracks/recorder`.
- Python ≥ 3.10, ideally with [`uv`](https://docs.astral.sh/uv/).

## Installing in Hermes

### 1. Register the MCP server

In `~/.hermes/config.yaml` (full example: [`examples/hermes-config.yaml`](examples/hermes-config.yaml)):

```yaml
mcp_servers:
  owntracks:                 # keep this name, the skill expects mcp_owntracks_* tools
    command: uvx
    args: ["--from", "git+https://github.com/de-niji/OwnTracksHermes", "owntracks-mcp"]
    env:
      OWNTRACKS_URL: "http://localhost:8083"
      OWNTRACKS_TIMEZONE: "Europe/Berlin"
      OWNTRACKS_DEFAULT_USER: "alice"
      OWNTRACKS_DEFAULT_DEVICE: "phone"
      OWNTRACKS_HOME: "51.9607,7.6261"
    timeout: 60
```

If the GitHub repository is private, or you prefer a local install:

```bash
git clone https://github.com/de-niji/OwnTracksHermes ~/OwnTracksHermes
cd ~/OwnTracksHermes && uv venv && uv pip install .
# then in config.yaml:  command: /home/<you>/OwnTracksHermes/.venv/bin/owntracks-mcp
```

Put passwords in `~/.hermes/.env` and reference them in the config as `${OWNTRACKS_PASSWORD}`.

### 2. Install the skill

Either of these works:

```bash
# a) copy it
cp -r skills/owntracks ~/.hermes/skills/

# b) or load it straight from the cloned repo, in ~/.hermes/config.yaml:
# skills:
#   external_dirs:
#     - /home/<you>/OwnTracksHermes/skills
```

### 3. Test

```bash
hermes mcp test owntracks      # should report 6 tools
hermes chat
> Where am I right now?
```

## Configuration (environment variables)

| Variable | Required | Meaning |
|---|---|---|
| `OWNTRACKS_URL` | yes | Base URL of the Recorder, e.g. `http://localhost:8083` or `https://example.com/owntracks` (without `/api/0`) |
| `OWNTRACKS_USERNAME` / `OWNTRACKS_PASSWORD` | no | HTTP basic auth, if a reverse proxy (nginx, Caddy, Traefik) sits in front |
| `OWNTRACKS_VERIFY_SSL` | no | `true` (default), `false`, or the path to a CA bundle |
| `OWNTRACKS_TIMEZONE` | no | e.g. `Europe/Berlin`. Defaults to the system time zone |
| `OWNTRACKS_DEFAULT_USER` | no | Who "I" / `me` is |
| `OWNTRACKS_DEFAULT_DEVICE` | no | Default device of that user |
| `OWNTRACKS_HOME` | no | `lat,lon` of home, for "how far from home" |
| `OWNTRACKS_TIMEOUT` | no | HTTP timeout in seconds (default 15) |

Addresses only appear if the Recorder does reverse geocoding (`OTR_GEOKEY`).
Named places like "Home" or "Work" come from the **regions** (waypoints) set up in the OwnTracks app.

## Security

The OwnTracks Recorder has **no authentication of its own**. Anyone who can reach its API can
read, and even delete, all data. So:

- Keep the Recorder reachable only locally or over a VPN, or put a reverse proxy with basic
  auth in front of it (and use `OWNTRACKS_USERNAME` / `OWNTRACKS_PASSWORD`).
- This MCP server only exposes read-only tools, marks them with `readOnlyHint`, and never
  calls the delete endpoint.
- Location data is very personal. The skill instructs Hermes to share it only with the
  person asking.

## Running as an HTTP server (optional)

If Hermes runs on a different machine than the Recorder:

```bash
owntracks-mcp --transport streamable-http --host 0.0.0.0 --port 8765
```

```yaml
mcp_servers:
  owntracks:
    url: "http://recorder-host:8765/mcp"
```

Don't expose this port to the internet unprotected.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
```

The tests run against a simulated Recorder (`tests/fake_recorder.py`). One of them launches the
real server over stdio, the way Hermes does. Tested with MCP Python SDK 1.x and 2.x.

## License

MIT
