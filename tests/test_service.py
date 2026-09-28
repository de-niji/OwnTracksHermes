from zoneinfo import ZoneInfo

import httpx
import pytest

from fake_recorder import HOME, NOW, OFFICE, FakeRecorder, as_httpx_handler
from owntracks_mcp.client import RecorderClient, RecorderError
from owntracks_mcp.config import ConfigError, Settings
from owntracks_mcp.service import LookupError_, OwnTracksService


def make(**overrides):
    fake = FakeRecorder()
    settings = Settings(
        url="http://recorder.test",
        timezone=ZoneInfo("Europe/Berlin"),
        **overrides,
    )
    client = RecorderClient(settings, transport=httpx.MockTransport(as_httpx_handler(fake)))
    return fake, OwnTracksService(settings, client, clock=lambda: NOW)


async def test_status_and_list():
    _, svc = make()
    status = await svc.status()
    assert status["reachable"] and status["recorder_version"] == "0.9.9-fake"
    assert status["users"] == ["nico", "anna"]
    devices = await svc.list_devices()
    assert {"user": "anna", "devices": ["iphone", "ipad"]} in devices["users"]


async def test_last_location_everyone_sorted_by_age():
    _, svc = make()
    locs = (await svc.last_location())["locations"]
    assert [(loc["user"], loc["device"]) for loc in locs] == [
        ("anna", "iphone"), ("nico", "pixel"), ("anna", "ipad"),
    ]
    assert locs[0]["address"] == "Marienplatz, München"
    assert locs[0]["age"] == "2 min ago"


async def test_last_location_case_insensitive_user():
    _, svc = make()
    locs = (await svc.last_location("ANNA"))["locations"]
    assert {loc["device"] for loc in locs} == {"iphone", "ipad"}


async def test_me_uses_default_user_and_device():
    fake, svc = make(default_user="nico", default_device="pixel", home=HOME)
    locs = (await svc.last_location("me"))["locations"]
    assert len(locs) == 1 and locs[0]["device"] == "pixel"
    assert 3 < locs[0]["distance_from_home_km"] < 4
    assert ("last", {"user": "nico", "device": "pixel"}) in fake.requests


async def test_me_without_default_is_helpful():
    _, svc = make()
    with pytest.raises(LookupError_, match="Known users: nico, anna"):
        await svc.last_location("me")


async def test_unknown_user():
    _, svc = make()
    with pytest.raises(LookupError_, match="Unknown user 'bob'"):
        await svc.last_location("bob")


async def test_history_requires_device_choice_when_ambiguous():
    _, svc = make()
    with pytest.raises(LookupError_, match="several devices"):
        await svc.history("anna", None, "today", None)


async def test_history_summary():
    fake, svc = make()
    h = await svc.history("nico", None, "today", None, max_points=10)
    assert h["device"] == "pixel"
    assert h["point_count"] == 49
    assert 3.0 < h["distance_km"] < 4.0
    assert h["max_speed_kmh"] == 35
    assert len(h["points"]) == 10
    assert "thinned" in h["points_note"]
    assert [s.get("regions") for s in h["stays"]] == [["Home"], ["Work"]]
    assert h["stays"][0]["from"] == "2026-09-28T08:00:00+02:00"
    assert h["stays"][1]["address"] == "Bürostraße 5, Münster"
    # 'today' in Berlin starts at 22:00 UTC the day before
    verb, q = fake.requests[-1]
    assert verb == "locations" and q["from"] == "2026-09-27T22:00:00" and q["to"] == "2026-09-28T12:00:00"


async def test_history_exact_window_and_empty_range():
    _, svc = make()
    h = await svc.history("nico", "pixel", "2026-09-28T09:05", "2026-09-28T09:10")
    assert h["point_count"] == 6
    assert h["stays"] == []
    empty = await svc.history("nico", "pixel", "yesterday", "yesterday")
    assert empty["point_count"] == 0 and "No locations" in empty["note"]


async def test_history_rejects_reversed_range():
    _, svc = make()
    with pytest.raises(ValueError, match="must be after"):
        await svc.history("nico", "pixel", "now", "2d")


async def test_distance_to_point_and_home():
    _, svc = make(home=HOME)
    d = await svc.distance(OFFICE[0], OFFICE[1], "nico")
    assert d["results"][0]["distance_km"] == 0
    assert d["results"][0]["within_accuracy"] is True
    home = await svc.distance(user="nico")
    assert home["target"] == "home" and 3 < home["results"][0]["distance_km"] < 4


async def test_distance_without_home_errors():
    _, svc = make()
    with pytest.raises(ValueError, match="OWNTRACKS_HOME"):
        await svc.distance()


async def test_reverse_geocode():
    _, svc = make()
    hit = await svc.reverse_geocode(51.9607, 7.6261)
    assert hit["found"] and hit["address"].startswith("Prinzipalmarkt")
    miss = await svc.reverse_geocode(0, 0)
    assert miss["found"] is False


async def test_http_errors_are_explained():
    settings = Settings(url="http://recorder.test")

    def deny(request):
        return httpx.Response(401)

    svc = OwnTracksService(settings, RecorderClient(settings, transport=httpx.MockTransport(deny)))
    with pytest.raises(RecorderError, match="OWNTRACKS_USERNAME"):
        await svc.list_devices()

    def html(request):
        return httpx.Response(200, text="<html>login</html>")

    svc = OwnTracksService(settings, RecorderClient(settings, transport=httpx.MockTransport(html)))
    with pytest.raises(RecorderError, match="non-JSON"):
        await svc.list_devices()


def test_settings_from_env():
    s = Settings.from_env({
        "OWNTRACKS_URL": "https://ot.example.com/owntracks/api/0/",
        "OWNTRACKS_USERNAME": "u",
        "OWNTRACKS_PASSWORD": "p",
        "OWNTRACKS_VERIFY_SSL": "false",
        "OWNTRACKS_TIMEZONE": "Europe/Berlin",
        "OWNTRACKS_HOME": "51.96, 7.62",
    })
    assert s.api_base == "https://ot.example.com/owntracks/api/0"
    assert s.verify is False and s.home == (51.96, 7.62)
    with pytest.raises(ConfigError, match="OWNTRACKS_URL"):
        Settings.from_env({})
    unresolved = Settings.from_env({
        "OWNTRACKS_URL": "http://x", "OWNTRACKS_USERNAME": "${OWNTRACKS_USERNAME}",
        "OWNTRACKS_PASSWORD": "${env:OWNTRACKS_PASSWORD}", "OWNTRACKS_TIMEZONE": " ",
    })
    assert unresolved.username is None and unresolved.password is None and unresolved.timezone is None
    with pytest.raises(ConfigError, match="OWNTRACKS_HOME"):
        Settings.from_env({"OWNTRACKS_URL": "http://x", "OWNTRACKS_HOME": "home"})
