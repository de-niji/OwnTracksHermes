import json
import os
import stat
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from fake_recorder import HOME, NOW, OFFICE, FakeRecorder, _rec, as_httpx_handler
from owntracks_mcp import reminders as rem
from owntracks_mcp.client import RecorderClient
from owntracks_mcp.config import Settings
from owntracks_mcp.service import LookupError_, OwnTracksService

EDEKA = (51.9550, 7.6100)  # ~1.4 km from HOME


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def tick(self, minutes=1):
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def env(tmp_path):
    fake = FakeRecorder()
    clock = Clock()
    settings = Settings(
        url="http://recorder.test", timezone=ZoneInfo("Europe/Berlin"),
        default_user="nico", default_device="pixel", home=HOME,
    )
    store = rem.ReminderStore(tmp_path / "owntracks")
    client = RecorderClient(settings, transport=httpx.MockTransport(as_httpx_handler(fake)))
    svc = OwnTracksService(
        settings, client, clock=clock, store=store,
        recorder_env={"OWNTRACKS_URL": "http://recorder.test", "OWNTRACKS_PASSWORD": "pw"},
    )
    return fake, clock, svc


def move(fake, clock, user, device, lat, lon, **extra):
    fake.last[(user, device)] = _rec(int(clock().timestamp()), lat, lon, user, device, **extra)


# -- pure helpers ---------------------------------------------------------------


def test_is_inside_circle_with_hysteresis():
    place = {"kind": "circle", "lat": HOME[0], "lon": HOME[1], "radius_m": 100}
    at = lambda dlat, acc=10: {"lat": HOME[0] + dlat, "lon": HOME[1], "acc": acc}  # noqa: E731
    assert rem.is_inside(place, at(0)) is True
    assert rem.is_inside(place, at(0.0008)) is True       # ~89 m
    assert rem.is_inside(place, at(0.0012)) is None       # ~133 m: inside hysteresis band
    assert rem.is_inside(place, at(0.0020)) is False      # ~222 m
    assert rem.is_inside(place, at(0, acc=5000)) is None  # too inaccurate


def test_is_inside_region_is_case_insensitive():
    place = {"kind": "region", "name": "Work"}
    assert rem.is_inside(place, {"inregions": ["work"]}) is True
    assert rem.is_inside(place, {"inregions": ["Home"]}) is False
    assert rem.is_inside(place, {}) is False


@pytest.mark.parametrize(
    "trigger, prev, cur, expected",
    [
        ("enter", False, True, True),
        ("enter", None, True, True),
        ("enter", True, True, False),
        ("enter", False, None, False),
        ("leave", True, False, True),
        ("leave", None, False, False),
        ("leave", False, False, False),
    ],
)
def test_fires(trigger, prev, cur, expected):
    assert rem.fires(trigger, prev, cur) is expected


def test_pick_record_prefers_freshest_device():
    recs = [
        {"tst": 10, "topic": "owntracks/anna/ipad"},
        {"tst": 20, "topic": "owntracks/anna/iphone"},
    ]
    assert rem.pick_record(recs, None)["tst"] == 20
    assert rem.pick_record(recs, "ipad")["tst"] == 10
    assert rem.pick_record([], None) is None


# -- service ------------------------------------------------------------------------


async def test_arrive_at_coordinates_fires_once(env):
    fake, clock, svc = env
    out = await svc.create_reminder("Buy milk", "Edeka", lat=EDEKA[0], lon=EDEKA[1])
    r = out["created"]
    assert r["trigger"] == "arrive" and r["user"] == "nico" and r["currently_inside"] is False
    assert any("setup-reminders" in n for n in out["notes"])

    assert await svc.check_reminders() == []
    clock.tick()
    move(fake, clock, "nico", "pixel", *EDEKA)
    [msg] = await svc.check_reminders()
    assert msg == "📍 Buy milk\n(You arrived at Edeka, 14:01)"

    clock.tick()
    move(fake, clock, "nico", "pixel", *HOME)
    clock.tick()
    move(fake, clock, "nico", "pixel", *EDEKA)
    assert await svc.check_reminders() == []  # one-shot
    listed = await svc.list_reminders(include_inactive=True)
    assert listed["reminders"][0]["active"] is False
    assert listed["reminders"][0]["fired_count"] == 1
    assert listed["checker_running"] is True


async def test_already_there_waits_for_next_arrival(env):
    fake, clock, svc = env
    out = await svc.create_reminder("Take the parcel", "Work", trigger="leave")
    assert out["created"]["currently_inside"] is True  # nico's last fix is in region Work
    await svc.create_reminder("Say hi", "Work")  # arrive, but already there
    assert await svc.check_reminders() == []

    clock.tick()
    move(fake, clock, "nico", "pixel", *HOME, inregions=["Home"])
    [msg] = await svc.check_reminders()
    assert msg.startswith("📍 Take the parcel\n(You left Work")

    clock.tick()
    move(fake, clock, "nico", "pixel", *OFFICE, inregions=["Work"])
    [msg] = await svc.check_reminders()
    assert msg.startswith("📍 Say hi\n(You arrived at Work")


async def test_repeating_reminder_for_another_user(env):
    fake, clock, svc = env
    await svc.create_reminder("Anna is home", "home", user="anna", repeat=True)
    for _ in range(2):
        clock.tick()
        move(fake, clock, "anna", "iphone", *HOME)
        [msg] = await svc.check_reminders()
        assert msg == f"📍 Anna is home\n(Anna arrived at home, {clock().astimezone(ZoneInfo('Europe/Berlin')):%H:%M})"
        clock.tick()
        move(fake, clock, "anna", "iphone", *EDEKA)
        assert await svc.check_reminders() == []
    assert (await svc.list_reminders())["reminders"][0]["fired_count"] == 2


async def test_expiry(env):
    fake, clock, svc = env
    out = await svc.create_reminder("Buy milk", "Edeka", lat=EDEKA[0], lon=EDEKA[1], expires="30m")
    assert out["created"]["expires"] == "2026-09-28T14:30:00+02:00"
    clock.tick(31)
    move(fake, clock, "nico", "pixel", *EDEKA)
    assert await svc.check_reminders() == []
    assert (await svc.list_reminders())["reminders"] == []
    with pytest.raises(ValueError, match="in the past"):
        await svc.create_reminder("x", "Edeka", lat=1, lon=1, expires="yesterday")


async def test_validation(env):
    _, _, svc = env
    with pytest.raises(ValueError, match="arrive' or 'leave"):
        await svc.create_reminder("x", "Work", trigger="sometimes")
    with pytest.raises(ValueError, match="needs a text"):
        await svc.create_reminder("  ", "Work")
    with pytest.raises(ValueError, match="both lat and lon"):
        await svc.create_reminder("x", "Shop", lat=1.0)
    with pytest.raises(LookupError_, match="Unknown user"):
        await svc.create_reminder("x", "Work", user="bob")


async def test_delete(env):
    _, _, svc = env
    rid = (await svc.create_reminder("x", "Work"))["created"]["id"]
    assert await svc.delete_reminder(rid) == {"deleted": rid}
    with pytest.raises(ValueError, match="No reminder"):
        await svc.delete_reminder(rid)


async def test_recorder_outage_alerts_once_and_recovers(env):
    fake, clock, svc = env
    await svc.create_reminder("x", "Work")
    healthy = svc.client

    def down(request):
        raise httpx.ConnectError("connection refused")

    svc.client = RecorderClient(svc.settings, transport=httpx.MockTransport(down))
    outputs = [await svc.check_reminders() for _ in range(rem.FAILURE_ALERT_THRESHOLD + 5)]
    alerts = [m for out in outputs for m in out]
    assert len(alerts) == 1 and "unreachable" in alerts[0]

    svc.client = healthy
    assert await svc.check_reminders() == ["✅ OwnTracks reminders: the Recorder is reachable again."]
    assert await svc.check_reminders() == []


async def test_checker_config_is_private(env, tmp_path):
    _, _, svc = env
    await svc.create_reminder("x", "Work")
    cfg = svc.store.config_path
    assert json.loads(cfg.read_text())["OWNTRACKS_PASSWORD"] == "pw"
    if os.name == "posix":
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_setup_reminders_writes_script(tmp_path, capsys):
    from owntracks_mcp.checker import SCRIPT_NAME, setup_reminders

    assert setup_reminders(tmp_path / "hermes", tmp_path / "data", "signal:+4912345") == 0
    script = tmp_path / "hermes" / "scripts" / SCRIPT_NAME
    text = script.read_text()
    assert "check-reminders" in text and f"OWNTRACKS_DATA_DIR={tmp_path / 'data'}" in text
    assert os.access(script, os.X_OK)
    out = capsys.readouterr().out
    assert "hermes cron create" in out and "signal:+4912345" in out


def test_check_without_reminders_is_silent(tmp_path, capsys):
    from owntracks_mcp.checker import run_check

    assert run_check(tmp_path / "nothing") == 0
    assert capsys.readouterr().out == ""
