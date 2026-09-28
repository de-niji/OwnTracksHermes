"""Pure helpers: distances, time parsing, record formatting, trip analysis."""

from __future__ import annotations

import math
import re
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable

EARTH_RADIUS_M = 6_371_000.0

# OwnTracks "conn" field
_CONNECTION = {"w": "wifi", "m": "mobile", "o": "offline"}
# OwnTracks "t" (trigger) field
_TRIGGER = {
    "p": "ping",
    "c": "region",
    "b": "beacon",
    "r": "requested",
    "u": "manual",
    "t": "timer",
    "v": "frequent-locations",
}
# OwnTracks "bs" (battery status) field
_BATTERY_STATUS = {0: "unknown", 1: "unplugged", 2: "charging", 3: "full"}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def local_tz(tz: tzinfo | None) -> tzinfo:
    return tz or datetime.now().astimezone().tzinfo or timezone.utc


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

_RELATIVE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|min|h|d|w)\s*(?:ago)?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_time(value: str | None, *, tz: tzinfo | None, now: datetime, end_of_day: bool = False) -> datetime:
    """Parse a user/LLM supplied time into an aware datetime.

    Accepts ``now``, ``today``, ``yesterday``, relative offsets like ``30m``,
    ``6h``, ``2d``, ``1w`` (meaning "that long ago"), plain dates
    (``2026-09-27``) and ISO 8601 timestamps. Naive values are interpreted in
    ``tz`` (or the system time zone). ``end_of_day`` makes date-only values
    resolve to the end of that day instead of its start.
    """
    zone = local_tz(tz)
    if value is None or not value.strip() or value.strip().lower() == "now":
        return now
    v = value.strip()
    lower = v.lower()

    if lower in ("today", "yesterday"):
        day = now.astimezone(zone).date()
        if lower == "yesterday":
            day -= timedelta(days=1)
        return _day_bound(day, zone, end_of_day)

    m = _RELATIVE.match(v)
    if m:
        amount, unit = float(m.group(1)), m.group(2).lower()
        return now - timedelta(seconds=amount * _UNIT_SECONDS[unit])

    if _DATE_ONLY.match(v):
        day = datetime.strptime(v, "%Y-%m-%d").date()
        return _day_bound(day, zone, end_of_day)

    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Could not understand time {value!r}. Use e.g. 'now', 'today', 'yesterday', "
            "'6h', '2d', '2026-09-27' or '2026-09-27T14:30'."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt


def _day_bound(day: Any, zone: tzinfo, end_of_day: bool) -> datetime:
    if end_of_day:
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone) - timedelta(seconds=1)
    return datetime.combine(day, time.min, tzinfo=zone)


def to_recorder_time(dt: datetime) -> str:
    """The Recorder expects UTC in the form YYYY-MM-DDTHH:MM:SS."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def format_ts(tst: int | float | None, tz: tzinfo | None) -> str | None:
    if tst is None:
        return None
    return datetime.fromtimestamp(float(tst), tz=local_tz(tz)).isoformat(timespec="seconds")


def humanize_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min ago"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.1f} h ago"
    return f"{hours / 24:.1f} days ago"


# ---------------------------------------------------------------------------
# Record formatting
# ---------------------------------------------------------------------------


def osm_url(lat: float, lon: float, zoom: int = 17) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map={zoom}/{lat:.6f}/{lon:.6f}"


def num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def user_device_from(rec: dict[str, Any]) -> tuple[str | None, str | None]:
    user, device = rec.get("username"), rec.get("device")
    if (not user or not device) and isinstance(rec.get("topic"), str):
        parts = rec["topic"].split("/")
        # owntracks/<user>/<device>
        if len(parts) >= 3:
            user = user or parts[1]
            device = device or parts[2]
    return user, device


def format_location(
    rec: dict[str, Any],
    *,
    tz: tzinfo | None,
    now: datetime,
    home: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Turn a raw OwnTracks location record into a compact, LLM-friendly dict."""
    lat, lon = num(rec.get("lat")), num(rec.get("lon"))
    tst = num(rec.get("tst"))
    user, device = user_device_from(rec)

    out: dict[str, Any] = {
        "user": user,
        "device": device,
        "tid": rec.get("tid"),
        "lat": lat,
        "lon": lon,
        "timestamp": format_ts(tst, tz),
    }
    if tst is not None:
        age = now.timestamp() - tst
        out["age"] = humanize_age(age)
        out["age_seconds"] = int(age)

    address = rec.get("addr") or rec.get("display_name")
    if address:
        out["address"] = address
    if rec.get("locality"):
        out["locality"] = rec["locality"]
    if rec.get("cc"):
        out["country_code"] = rec["cc"]
    if rec.get("inregions"):
        out["in_regions"] = rec["inregions"]

    acc = num(rec.get("acc"))
    if acc is not None:
        out["accuracy_m"] = round(acc)
    alt = num(rec.get("alt"))
    if alt is not None:
        out["altitude_m"] = round(alt)
    vel = num(rec.get("vel"))
    if vel is not None:
        out["speed_kmh"] = round(vel)
    cog = num(rec.get("cog"))
    if cog is not None:
        out["course_deg"] = round(cog)
    batt = num(rec.get("batt"))
    if batt is not None:
        out["battery_pct"] = round(batt)
    if rec.get("bs") is not None:
        out["battery_status"] = _BATTERY_STATUS.get(rec.get("bs"), str(rec.get("bs")))
    if rec.get("conn"):
        out["connection"] = _CONNECTION.get(rec["conn"], rec["conn"])
    if rec.get("t"):
        out["trigger"] = _TRIGGER.get(rec["t"], rec["t"])
    if rec.get("SSID"):
        out["wifi_ssid"] = rec["SSID"]

    if lat is not None and lon is not None:
        out["map_url"] = osm_url(lat, lon)
        if home is not None:
            out["distance_from_home_km"] = round(haversine_m(lat, lon, *home) / 1000, 3)

    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------------------
# Track analysis
# ---------------------------------------------------------------------------


def clean_track(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only location records with coordinates and a timestamp, sorted by time."""
    points = []
    for rec in records:
        if rec.get("_type") not in (None, "location"):
            continue
        lat, lon, tst = num(rec.get("lat")), num(rec.get("lon")), num(rec.get("tst"))
        if lat is None or lon is None or tst is None:
            continue
        points.append({**rec, "lat": lat, "lon": lon, "tst": tst})
    points.sort(key=lambda r: r["tst"])
    return points


def track_distance_m(points: list[dict[str, Any]], max_accuracy_m: float = 500) -> float:
    """Sum of leg distances, ignoring very inaccurate fixes."""
    usable = [p for p in points if (num(p.get("acc")) or 0) <= max_accuracy_m]
    return sum(
        haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(usable, usable[1:])
    )


def downsample(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    """Evenly thin out a track, always keeping the first and last point."""
    n = len(points)
    if max_points <= 0:
        return []
    if n <= max_points:
        return list(points)
    if max_points == 1:
        return [points[-1]]
    step = (n - 1) / (max_points - 1)
    return [points[round(i * step)] for i in range(max_points)]


def detect_stays(
    points: list[dict[str, Any]],
    *,
    radius_m: float = 150,
    min_duration_s: float = 600,
) -> list[dict[str, Any]]:
    """Find places where the device stayed within ``radius_m`` for at least ``min_duration_s``.

    Classic stay-point detection: grow a window from an anchor point while the
    following points remain within the radius of the anchor.
    """
    stays: list[dict[str, Any]] = []
    i, n = 0, len(points)
    while i < n:
        j = i + 1
        while j < n and haversine_m(
            points[i]["lat"], points[i]["lon"], points[j]["lat"], points[j]["lon"]
        ) <= radius_m:
            j += 1
        window = points[i:j]
        duration = window[-1]["tst"] - window[0]["tst"]
        if duration >= min_duration_s:
            lat = sum(p["lat"] for p in window) / len(window)
            lon = sum(p["lon"] for p in window) / len(window)
            addr = next((p.get("addr") for p in window if p.get("addr")), None)
            regions = sorted({r for p in window for r in (p.get("inregions") or [])})
            stays.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "start_tst": window[0]["tst"],
                    "end_tst": window[-1]["tst"],
                    "duration_s": duration,
                    "address": addr,
                    "regions": regions,
                    "points": len(window),
                }
            )
            i = j
        else:
            i += 1
    return stays
