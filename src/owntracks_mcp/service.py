"""Business logic behind the MCP tools, independent of the MCP SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from . import geo
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
    ):
        self.settings = settings
        self.client = client
        self.clock = clock

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
