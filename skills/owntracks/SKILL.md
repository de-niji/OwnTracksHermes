---
name: owntracks
description: Answer "where is / where was / how far" questions and set "remind me when I arrive at / leave …" reminders from OwnTracks location data (via the owntracks MCP server).
version: 0.2.0
author: OwnTracksHermes
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [OwnTracks, Location, GPS, Tracking, MCP]
    category: productivity
    requires_tools: [mcp_owntracks_get_last_location]
    related_skills: []
---

# OwnTracks location

Location data from the user's own OwnTracks Recorder, exposed by the `owntracks`
MCP server. Everything is read-only except the location-reminder tools.

## When to Use

- "Where am I / where is Anna / where's my phone?"
- "Where was I yesterday afternoon?", "When did I leave work?", "What route did I take?"
- "How far is Anna from home / from <place>?", "Is Alice already home?"
- "How much did I drive this week?", "Is my phone's battery low?"
- "Remind me to buy milk when I'm at Edeka", "When I leave work, remind me to take the parcel",
  "Tell me when Anna gets home", "What location reminders do I have?"

## Tools

| Tool | Use for |
|---|---|
| `mcp_owntracks_get_last_location(user?, device?)` | Current position. No user → everyone. `user="me"` → configured default user. |
| `mcp_owntracks_get_location_history(user, device?, start?, end?, max_points?, stay_min_minutes?)` | Past positions: `stays` (where and how long), `distance_km`, thinned `points` route. |
| `mcp_owntracks_get_distance(lat?, lon?, user?, device?)` | Straight-line distance from the latest position to a point, or to home when lat/lon are omitted. |
| `mcp_owntracks_list_devices()` | Discover user and device names. |
| `mcp_owntracks_reverse_geocode(lat, lon)` | Address from the Recorder's geocache (only places it has already seen). |
| `mcp_owntracks_owntracks_status()` | Connection check / troubleshooting (also shows whether the reminder checker runs). |
| `mcp_owntracks_create_location_reminder(text, place, trigger?, user?, device?, lat?, lon?, radius_m?, repeat?, expires?)` | Reminder that fires when someone arrives at (`arrive`) or leaves (`leave`) a place. |
| `mcp_owntracks_list_location_reminders(include_inactive?)` | Show reminders and checker health. |
| `mcp_owntracks_delete_location_reminder(reminder_id)` | Remove a reminder. |

Time arguments accept `today`, `yesterday`, `now`, relative `30m` / `6h` / `2d` / `1w`
(meaning "that long ago"), dates `2026-09-27` (as `end` = end of that day) and ISO
times `2026-09-27T14:30` (local time zone).

## Procedure

1. Map the person in the question to an OwnTracks user. "I/me/my" → `user="me"`.
   If a name is unknown or ambiguous, call `list_devices` once and match case-insensitively
   (users are often lowercase first names).
2. "Where is X now" → `get_last_location`. Always report **how old** the position is
   (`age`) and its accuracy if it is worse than ~100 m. A position several hours old
   is not "now" — say so.
3. Prefer `address`, `locality` or `in_regions` (named OwnTracks regions like "Home",
   "Work") over raw coordinates. Add `map_url` when the user may want to look at it.
4. "Where was X / what did X do" → `get_location_history` with a fitting range, then
   summarise the `stays` chronologically ("08:00–09:00 at home, 09:15–11:00 at work").
   Use a small `max_points` (default 50) unless the route itself matters.
5. For places without coordinates ("how far from the main station"), resolve the place
   to lat/lon with your other tools first (e.g. web search), then call `get_distance`.
   The result is straight-line distance, not travel distance — say so.

## Location reminders

1. Pick the place:
   - A region the user defined in the OwnTracks app ("Work", "Gym") → `place="Work"`.
     Regions fire most reliably, because the phone reports entering/leaving them immediately.
   - Home → `place="home"` (needs `OWNTRACKS_HOME`).
   - A shop or address → resolve it to coordinates first (web search / maps), then pass
     `lat`, `lon` and a short label as `place`. Ask which branch if a chain is ambiguous
     ("which Edeka?"). Default radius 150 m; use 200–300 m for big sites.
2. `trigger="arrive"` for "when I'm at / get to", `"leave"` for "when I leave".
   `repeat=true` only if the user says "every time" / "always". Use `expires` for
   "today" / "this week" (`expires="today"`, `"3d"` = in three days).
3. Keep `text` in the user's own words and language; it is sent verbatim.
4. Relay the result's `notes` to the user, especially:
   - "Already there right now" → the reminder waits for the next arrival; mention the
     reminder text now if it is useful.
   - "only delivered while the checker cron job runs" → tell the user to run
     `owntracks-mcp setup-reminders` once and create the cron job it prints.
5. Reminders about other people ("tell me when Anna gets home") only work for users in
   the same Recorder, i.e. people who already share their location with the user.

## Pitfalls

- A tool error saying "several devices" means pass `device=` (list is in the message).
- "Could not reach the OwnTracks Recorder" / HTTP 401 → configuration problem; run
  `owntracks_status` and tell the user to check `OWNTRACKS_URL` / credentials in
  `~/.hermes/config.yaml` or `~/.hermes/.env`.
- `address` only exists if the Recorder does reverse geocoding (`OTR_GEOKEY`).
- `distance_from_home_km` only appears when `OWNTRACKS_HOME` is configured.

## Privacy

Location history is sensitive personal data. Only answer for the person asking or
people who clearly share their location with them; never post locations into group
chats or third-party services unless the user explicitly asks.

## Verification

`mcp_owntracks_owntracks_status` returns `reachable: true` and the list of users.
