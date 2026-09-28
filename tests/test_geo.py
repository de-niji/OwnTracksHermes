from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from owntracks_mcp import geo

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_haversine_known_distance():
    # Münster Prinzipalmarkt -> Münster Hbf is roughly 1.1 km
    d = geo.haversine_m(51.9620, 7.6280, 51.9567, 7.6352)
    assert 700 < d < 900
    assert geo.haversine_m(10, 10, 10, 10) == 0


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, NOW),
        ("now", NOW),
        ("6h", datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)),
        ("2d", datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)),
        ("30 min ago", datetime(2026, 9, 28, 11, 30, tzinfo=timezone.utc)),
        ("today", datetime(2026, 9, 28, 0, 0, tzinfo=BERLIN)),
        ("yesterday", datetime(2026, 9, 27, 0, 0, tzinfo=BERLIN)),
        ("2026-09-01", datetime(2026, 9, 1, 0, 0, tzinfo=BERLIN)),
        ("2026-09-01T14:30", datetime(2026, 9, 1, 14, 30, tzinfo=BERLIN)),
        ("2026-09-01T14:30:00Z", datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)),
    ],
)
def test_parse_time(value, expected):
    assert geo.parse_time(value, tz=BERLIN, now=NOW) == expected


def test_parse_time_end_of_day():
    end = geo.parse_time("yesterday", tz=BERLIN, now=NOW, end_of_day=True)
    assert end == datetime(2026, 9, 27, 23, 59, 59, tzinfo=BERLIN)


def test_parse_time_rejects_garbage():
    with pytest.raises(ValueError, match="Could not understand"):
        geo.parse_time("last tuesday-ish", tz=BERLIN, now=NOW)


def test_recorder_time_is_utc():
    assert geo.to_recorder_time(datetime(2026, 9, 28, 8, 0, tzinfo=BERLIN)) == "2026-09-28T06:00:00"


def test_format_location_maps_fields():
    rec = {
        "_type": "location", "tst": NOW.timestamp() - 300, "lat": 51.96, "lon": 7.62,
        "acc": 10.4, "batt": 55, "bs": 2, "conn": "w", "t": "u", "vel": 0,
        "topic": "owntracks/nico/pixel", "addr": "Somewhere 1", "inregions": ["Home"],
    }
    out = geo.format_location(rec, tz=BERLIN, now=NOW, home=(51.96, 7.62))
    assert out["user"] == "nico" and out["device"] == "pixel"
    assert out["timestamp"] == "2026-09-28T13:55:00+02:00"
    assert out["age"] == "5 min ago"
    assert out["battery_status"] == "charging"
    assert out["connection"] == "wifi"
    assert out["trigger"] == "manual"
    assert out["in_regions"] == ["Home"]
    assert out["distance_from_home_km"] == 0
    assert "openstreetmap.org" in out["map_url"]


def test_downsample_keeps_ends():
    pts = [{"i": i} for i in range(101)]
    s = geo.downsample(pts, 11)
    assert len(s) == 11
    assert s[0]["i"] == 0 and s[-1]["i"] == 100
    assert geo.downsample(pts, 500) == pts
    assert geo.downsample(pts, 0) == []


def test_clean_track_filters_and_sorts():
    raw = [
        {"_type": "location", "tst": 2, "lat": 1, "lon": 1},
        {"_type": "transition", "tst": 1, "lat": 1, "lon": 1},
        {"_type": "location", "tst": 1, "lat": "1.5", "lon": "1"},
        {"_type": "location", "tst": 3},
    ]
    cleaned = geo.clean_track(raw)
    assert [p["tst"] for p in cleaned] == [1, 2]
    assert cleaned[0]["lat"] == 1.5


def test_detect_stays():
    from fake_recorder import nico_track

    points = geo.clean_track(nico_track())
    stays = geo.detect_stays(points, radius_m=150, min_duration_s=600)
    assert len(stays) == 2
    home, office = stays
    assert home["regions"] == ["Home"] and home["duration_s"] >= 3600
    assert office["address"].startswith("Bürostraße")
    assert office["duration_s"] >= 6000
