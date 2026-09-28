"""Business logic behind the MCP tools, independent of the MCP SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from . import geo
from . import reminders as rem
from .client import RecorderClient
from .config import Settings

ME = "me"


class LookupError_(ValueError):
    """User/device could not be resolved. Message is meant for the agent."""


class OwnTracksService:
    def __init__(
        self,
        settings: Settings,
        client: RecorderClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        store: rem.ReminderStore | None = None,
        recorder_env: dict[str, str] | None = None,
    ):
        self.settings = settings
        self.client = client
        self.clock = clock
        self.store = store
        # OWNTRACKS_* settings handed to the cron checker (see ReminderStore).
        self.recorder_env = recorder_env or {}

    # -- resolution ---------------------------------------------------------

    async def _resolve_user(self, user: str | None) -> str:
        s = self.settings
        if user is None or user.strip().lower() in ("", ME):
            if s.default_user:
                return s.default_user
            users = await self.client.list_users()
            if len(users) == 1:
                return users[0]
            raise LookupError_(
                f"Please specify which user. Known users: {', '.join(users) or '(none)'}. "
                "(Tip: set OWNTRACKS_DEFAULT_USER so 'me' works.)"
            )
        users = await self.client.list_users()
        if user in users:
            return user
        for u in users:
            if u.lower() == user.strip().lower():
                return u
        raise LookupError_(f"Unknown user {user!r}. Known users: {', '.join(users) or '(none)'}")

    async def _resolve_device(self, user: str, device: str | None) -> str:
        devices = await self.client.list_devices(user)
        if device:
            if device in devices:
                return device
            for d in devices:
                if d.lower() == device.strip().lower():
                    return d
            raise LookupError_(
                f"User {user!r} has no device {device!r}. Devices: {', '.join(devices) or '(none)'}"
            )
        if self.settings.default_device and user == self.settings.default_user:
            if self.settings.default_device in devices or not devices:
                return self.settings.default_device
        if len(devices) == 1:
            return devices[0]
        if not devices:
            raise LookupError_(f"User {user!r} has no devices with recorded data.")
        raise LookupError_(
            f"User {user!r} has several devices, please pick one: {', '.join(devices)}"
        )

    def _fmt(self, rec: dict[str, Any], now: datetime) -> dict[str, Any]:
        return geo.format_location(rec, tz=self.settings.timezone, now=now, home=self.settings.home)

    # -- tools ----------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        s = self.settings
        out: dict[str, Any] = {
            "recorder_url": s.url,
            "auth": "basic" if s.username else "none",
            "timezone": str(geo.local_tz(s.timezone)),
            "default_user": s.default_user,
            "default_device": s.default_device,
            "home_configured": s.home is not None,
        }
        out["recorder_version"] = await self.client.version()
        out["users"] = await self.client.list_users()
        out["reachable"] = True
        if self.store is not None:
            data = self.store.read()
            out["reminders_file"] = str(self.store.path)
            out["active_reminders"] = sum(1 for r in data["reminders"] if r.get("active", True))
            out.update(rem.checker_health(data, self.clock().timestamp()))
        return out

    async def list_devices(self) -> dict[str, Any]:
        users = await self.client.list_users()
        result = []
        for u in users:
            result.append({"user": u, "devices": await self.client.list_devices(u)})
        return {
            "users": result,
            "default_user": self.settings.default_user,
            "default_device": self.settings.default_device,
        }

    async def last_location(self, user: str | None = None, device: str | None = None) -> dict[str, Any]:
        now = self.clock()
        if user is None and device is None:
            records = await self.client.last()
        else:
            u = await self._resolve_user(user)
            if device:
                d: str | None = await self._resolve_device(u, device)
            elif u == self.settings.default_user and self.settings.default_device:
                d = self.settings.default_device
            else:
                d = None  # all devices of this user
            records = await self.client.last(u, d)
        locations = [self._fmt(r, now) for r in records]
        locations.sort(key=lambda loc: loc.get("age_seconds", 1 << 62))
        if not locations:
            return {"locations": [], "note": "The Recorder has no location for this user/device yet."}
        return {"locations": locations}

    async def history(
        self,
        user: str | None,
        device: str | None,
        start: str | None,
        end: str | None,
        max_points: int = 50,
        stay_radius_m: float = 150,
        stay_min_minutes: float = 10,
    ) -> dict[str, Any]:
        now = self.clock()
        tz = self.settings.timezone
        start_dt = geo.parse_time(start or "24h", tz=tz, now=now)
        end_dt = geo.parse_time(end, tz=tz, now=now, end_of_day=True)
        if end_dt <= start_dt:
            raise ValueError(f"'end' ({end_dt.isoformat()}) must be after 'start' ({start_dt.isoformat()}).")

        u = await self._resolve_user(user)
        d = await self._resolve_device(u, device)
        raw = await self.client.locations(
            u, d, start=geo.to_recorder_time(start_dt), end=geo.to_recorder_time(end_dt)
        )
        points = geo.clean_track(raw)
        # The Recorder works on whole files; enforce the exact window ourselves.
        lo, hi = start_dt.timestamp(), end_dt.timestamp()
        points = [p for p in points if lo <= p["tst"] <= hi]

        out: dict[str, Any] = {
            "user": u,
            "device": d,
            "start": start_dt.astimezone(geo.local_tz(tz)).isoformat(timespec="seconds"),
            "end": end_dt.astimezone(geo.local_tz(tz)).isoformat(timespec="seconds"),
            "point_count": len(points),
        }
        if not points:
            out["note"] = "No locations recorded in this time range."
            return out

        speeds = [geo.num(p.get("vel")) for p in points]
        speeds = [v for v in speeds if v is not None]
        out["distance_km"] = round(geo.track_distance_m(points) / 1000, 2)
        if speeds:
            out["max_speed_kmh"] = round(max(speeds))
        out["first"] = self._fmt(points[0], now)
        out["last"] = self._fmt(points[-1], now)

        stays = geo.detect_stays(points, radius_m=stay_radius_m, min_duration_s=stay_min_minutes * 60)
        out["stays"] = [
            {
                k: v
                for k, v in {
                    "from": geo.format_ts(s["start_tst"], tz),
                    "to": geo.format_ts(s["end_tst"], tz),
                    "duration_min": round(s["duration_s"] / 60),
                    "address": s["address"],
                    "regions": s["regions"] or None,
                    "lat": round(s["lat"], 6),
                    "lon": round(s["lon"], 6),
                    "map_url": geo.osm_url(s["lat"], s["lon"]),
                }.items()
                if v is not None
            }
            for s in stays
        ]

        max_points = max(0, min(int(max_points), 1000))
        sampled = geo.downsample(points, max_points)
        out["points"] = [_compact_point(p, tz) for p in sampled]
        if len(sampled) < len(points):
            out["points_note"] = f"Track thinned out to {len(sampled)} of {len(points)} points."
        return out

    async def distance(
        self,
        lat: float | None = None,
        lon: float | None = None,
        user: str | None = None,
        device: str | None = None,
    ) -> dict[str, Any]:
        if lat is None or lon is None:
            if lat is not None or lon is not None:
                raise ValueError("Give both lat and lon, or neither (to measure from home).")
            if self.settings.home is None:
                raise ValueError("No target given and OWNTRACKS_HOME is not configured.")
            lat, lon = self.settings.home
            target = "home"
        else:
            target = f"{lat:.6f},{lon:.6f}"

        locs = (await self.last_location(user, device))["locations"]
        results = []
        for loc in locs:
            if "lat" not in loc or "lon" not in loc:
                continue
            meters = geo.haversine_m(loc["lat"], loc["lon"], lat, lon)
            results.append(
                {
                    "user": loc.get("user"),
                    "device": loc.get("device"),
                    "distance_km": round(meters / 1000, 3),
                    "within_accuracy": meters <= loc.get("accuracy_m", 0),
                    "position_age": loc.get("age"),
                    "position_timestamp": loc.get("timestamp"),
                    "address": loc.get("address"),
                }
            )
        results.sort(key=lambda r: r["distance_km"])
        return {"target": target, "results": [{k: v for k, v in r.items() if v is not None} for r in results]}

    async def reverse_geocode(self, lat: float, lon: float) -> dict[str, Any]:
        hit = await self.client.reverse_geocode(lat, lon)
        base = {"lat": lat, "lon": lon, "map_url": geo.osm_url(lat, lon)}
        if not hit:
            return {
                **base,
                "found": False,
                "note": "Not in the Recorder's geocache (only places the Recorder has already geocoded are known).",
            }
        return {**base, "found": True, "address": hit.get("addr"), "country_code": hit.get("cc")}

    # -- location reminders ---------------------------------------------------

    def _require_store(self) -> rem.ReminderStore:
        if self.store is None:
            raise ValueError("Reminders are not available (no data directory configured).")
        return self.store

    async def _current_record(self, user: str, device: str | None) -> dict[str, Any] | None:
        return rem.pick_record(await self.client.last(user, device), device)

    async def create_reminder(
        self,
        text: str,
        place: str,
        trigger: str = "enter",
        user: str | None = None,
        device: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 150,
        repeat: bool = False,
        expires: str | None = None,
    ) -> dict[str, Any]:
        store = self._require_store()
        now = self.clock()
        text = (text or "").strip()
        if not text:
            raise ValueError("The reminder needs a text.")
        trig = rem.TRIGGERS.get((trigger or "").strip().lower())
        if trig is None:
            raise ValueError("trigger must be 'arrive' or 'leave'.")
        if not 25 <= radius_m <= 50_000:
            raise ValueError("radius_m must be between 25 and 50000.")

        name = (place or "").strip()
        if (lat is None) != (lon is None):
            raise ValueError("Give both lat and lon, or neither.")
        if lat is not None:
            place_def: dict[str, Any] = {
                "kind": "circle", "lat": lat, "lon": lon, "radius_m": radius_m, "label": name or None,
            }
        elif name.lower() == "home":
            if self.settings.home is None:
                raise ValueError("OWNTRACKS_HOME is not configured; give lat/lon for home instead.")
            place_def = {
                "kind": "circle", "lat": self.settings.home[0], "lon": self.settings.home[1],
                "radius_m": radius_m, "label": "home",
            }
        elif name:
            place_def = {"kind": "region", "name": name}
        else:
            raise ValueError("Give a place: an OwnTracks region name, 'home', or a label plus lat/lon.")

        u = await self._resolve_user(user)
        d = await self._resolve_device(u, device) if device else None
        expires_tst = None
        if expires:
            expires_tst = geo.parse_time(
                expires, tz=self.settings.timezone, now=now, end_of_day=True, future=True
            ).timestamp()
            if expires_tst <= now.timestamp():
                raise ValueError("'expires' is in the past.")

        record = await self._current_record(u, d)
        inside = rem.is_inside(place_def, record) if record else None

        reminder = {
            "id": rem.new_id(),
            "text": text,
            "user": u,
            "device": d,
            "trigger": trig,
            "place": place_def,
            "repeat": bool(repeat),
            "expires_tst": expires_tst,
            "created_tst": now.timestamp(),
            "active": True,
            "inside": inside,
            "fired_count": 0,
        }
        with store.transaction() as data:
            data["reminders"].append(reminder)
            health = rem.checker_health(data, now.timestamp())
        store.save_recorder_config(self.recorder_env)

        out: dict[str, Any] = {"created": rem.describe(reminder, self.settings.timezone)}
        notes = []
        if inside and trig == "enter":
            notes.append("Already there right now; the reminder fires on the next arrival.")
        if inside is False and trig == "leave":
            notes.append("Not there right now; the reminder fires after arriving and then leaving.")
        if place_def["kind"] == "region":
            notes.append(
                f"'{name}' must match a region defined in the OwnTracks app (case-insensitive)."
            )
        if not health["checker_running"]:
            notes.append(
                "Reminders are only delivered while the checker cron job runs. "
                "Set it up once with: owntracks-mcp setup-reminders"
            )
        if notes:
            out["notes"] = notes
        return out

    async def list_reminders(self, include_inactive: bool = False) -> dict[str, Any]:
        store = self._require_store()
        data = store.read()
        items = [
            rem.describe(r, self.settings.timezone)
            for r in data["reminders"]
            if include_inactive or r.get("active", True)
        ]
        return {"reminders": items, **rem.checker_health(data, self.clock().timestamp())}

    async def delete_reminder(self, reminder_id: str) -> dict[str, Any]:
        store = self._require_store()
        with store.transaction() as data:
            before = len(data["reminders"])
            data["reminders"] = [r for r in data["reminders"] if r["id"] != reminder_id.strip()]
            removed = before - len(data["reminders"])
        if not removed:
            raise ValueError(f"No reminder with id {reminder_id!r}. Use list_location_reminders.")
        return {"deleted": reminder_id}

    async def check_reminders(self) -> list[str]:
        """One checker pass. Returns the messages to deliver (usually none)."""
        store = self._require_store()
        now_ts = self.clock().timestamp()
        messages: list[str] = []
        with store.transaction() as data:
            checker = data["checker"]
            checker["last_run"] = now_ts
            reminders = data["reminders"]

            # Expire and prune.
            for r in reminders:
                if r.get("active", True) and r.get("expires_tst") and now_ts > r["expires_tst"]:
                    r["active"] = False
                    r["ended_tst"] = now_ts
            data["reminders"] = reminders = [
                r for r in reminders
                if r.get("active", True) or now_ts - r.get("ended_tst", now_ts) < rem.KEEP_INACTIVE_S
            ]
            active = [r for r in reminders if r.get("active", True)]
            if not active:
                return messages

            try:
                by_user = {u: await self.client.last(u) for u in {r["user"] for r in active}}
            except Exception as exc:  # RecorderError, network trouble
                checker["failures"] = checker.get("failures", 0) + 1
                if checker["failures"] >= rem.FAILURE_ALERT_THRESHOLD and not checker.get("alerted"):
                    checker["alerted"] = True
                    messages.append(
                        f"⚠️ OwnTracks reminders: the Recorder has been unreachable for "
                        f"{checker['failures']} checks in a row ({exc})."
                    )
                return messages
            if checker.get("alerted"):
                messages.append("✅ OwnTracks reminders: the Recorder is reachable again.")
            checker["failures"] = 0
            checker["alerted"] = False

            for r in active:
                record = rem.pick_record(by_user.get(r["user"], []), r.get("device"))
                if record is None:
                    continue
                current = rem.is_inside(r["place"], record)
                if rem.fires(r["trigger"], r.get("inside"), current):
                    messages.append(
                        rem.format_message(
                            r, record, tz=self.settings.timezone, default_user=self.settings.default_user
                        )
                    )
                    r["fired_count"] = r.get("fired_count", 0) + 1
                    r["last_fired_tst"] = now_ts
                    if not r.get("repeat"):
                        r["active"] = False
                        r["ended_tst"] = now_ts
                if current is not None:
                    r["inside"] = current
        return messages


def _compact_point(p: dict[str, Any], tz: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "t": geo.format_ts(p["tst"], tz),
        "lat": round(p["lat"], 6),
        "lon": round(p["lon"], 6),
    }
    vel = geo.num(p.get("vel"))
    if vel:
        out["kmh"] = round(vel)
    if p.get("addr"):
        out["addr"] = p["addr"]
    return out
