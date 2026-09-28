"""Location-based reminders ("remind me of X when I arrive at / leave Y").

Reminders live in a small JSON file. The MCP server adds and removes them; a
separate checker (``owntracks-mcp check-reminders``, run every minute by a
Hermes no-agent cron job) compares the latest positions with them and prints a
message for every reminder that fires. Empty output means nothing happened,
which Hermes treats as a silent tick.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import geo

try:  # POSIX
    import fcntl
except ImportError:  # Windows: best effort without locking
    fcntl = None  # type: ignore[assignment]

TRIGGERS = {"enter": "enter", "arrive": "enter", "arrival": "enter", "leave": "leave", "exit": "leave"}

# After this many failed checks in a row (~minutes) the checker alerts once.
FAILURE_ALERT_THRESHOLD = 15
# After a long pause (checker off, phone offline) only replay this much history.
MAX_REPLAY_S = 6 * 3600
# Checker is considered not running when it has not run for this long.
CHECKER_STALE_S = 5 * 60
# Fired / expired reminders are kept this long for list_location_reminders.
KEEP_INACTIVE_S = 30 * 86400


def data_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("OWNTRACKS_DATA_DIR"):
        return Path(env["OWNTRACKS_DATA_DIR"]).expanduser()
    hermes_home = env.get("HERMES_HOME") or "~/.hermes"
    return Path(hermes_home).expanduser() / "owntracks"


def _empty() -> dict[str, Any]:
    return {"version": 1, "reminders": [], "checker": {}}


class ReminderStore:
    """JSON file guarded by a lock file, written atomically."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / "reminders.json"
        self.config_path = self.dir / "recorder.json"

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return _empty()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Reminder file {self.path} is corrupt: {exc}") from exc
        data.setdefault("reminders", [])
        data.setdefault("checker", {})
        return data

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "reminders.lock", "a+") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self.read()
                yield data
                _atomic_write(self.path, json.dumps(data, indent=2, ensure_ascii=False))
            finally:
                if fcntl is not None:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    # -- recorder settings for the checker -----------------------------------

    def save_recorder_config(self, cfg: dict[str, Any]) -> None:
        """Remember how to reach the Recorder, for the cron checker.

        Cron scripts run with a sanitized environment and do not see the MCP
        server's env, so the server leaves its settings here (mode 0600).
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(cfg, indent=2)
        try:
            if self.config_path.read_text("utf-8") == text:
                return
        except FileNotFoundError:
            pass
        _atomic_write(self.config_path, text, mode=0o600)

    def load_recorder_config(self) -> dict[str, str]:
        try:
            raw = json.loads(self.config_path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        return {k: str(v) for k, v in raw.items() if v is not None}


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def new_id() -> str:
    return secrets.token_hex(3)


def place_label(place: dict[str, Any]) -> str:
    return place.get("name") or place.get("label") or f"{place['lat']:.5f},{place['lon']:.5f}"


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def is_inside(place: dict[str, Any], rec: dict[str, Any]) -> bool | None:
    """Whether a location record is inside the place. None = can't tell (keep state)."""
    if place["kind"] == "region":
        regions = [str(r).lower() for r in (rec.get("inregions") or [])]
        return place["name"].lower() in regions

    lat, lon = geo.num(rec.get("lat")), geo.num(rec.get("lon"))
    if lat is None or lon is None:
        return None
    radius = float(place["radius_m"])
    acc = geo.num(rec.get("acc")) or 0
    if acc > max(2 * radius, 1000):
        return None  # fix too inaccurate to decide
    d = geo.haversine_m(lat, lon, place["lat"], place["lon"])
    if d <= radius:
        return True
    # Hysteresis so GPS jitter around the edge does not flip the state.
    if d > radius + max(50.0, 0.25 * radius):
        return False
    return None


def fires(trigger: str, previous: bool | None, current: bool | None) -> bool:
    if current is None:
        return False
    if trigger == "enter":
        return current and previous is not True
    return (not current) and previous is True


def pick_record(records: list[dict[str, Any]], device: str | None) -> dict[str, Any] | None:
    """The record for a specific device, or the freshest one of the user."""
    if device:
        records = [r for r in records if geo.user_device_from(r)[1] == device]
    records = [r for r in records if geo.num(r.get("tst")) is not None]
    return max(records, key=lambda r: geo.num(r["tst"]), default=None)


def describe(rem: dict[str, Any], tz: Any) -> dict[str, Any]:
    out = {
        "id": rem["id"],
        "text": rem["text"],
        "user": rem["user"],
        "device": rem.get("device"),
        "trigger": "arrive" if rem["trigger"] == "enter" else "leave",
        "place": place_label(rem["place"]),
        "place_kind": rem["place"]["kind"],
        "radius_m": rem["place"].get("radius_m"),
        "repeat": rem.get("repeat", False),
        "active": rem.get("active", True),
        "currently_inside": rem.get("inside"),
        "expires": geo.format_ts(rem.get("expires_tst"), tz),
        "created": geo.format_ts(rem.get("created_tst"), tz),
        "fired_count": rem.get("fired_count", 0),
        "last_fired": geo.format_ts(rem.get("last_fired_tst"), tz),
    }
    return {k: v for k, v in out.items() if v is not None}


def format_message(rem: dict[str, Any], rec: dict[str, Any], *, tz: Any, default_user: str | None) -> str:
    who = "You" if rem["user"] == default_user else rem["user"].capitalize()
    verb = "arrived at" if rem["trigger"] == "enter" else "left"
    tst = geo.num(rec.get("tst"))
    when = datetime.fromtimestamp(tst, tz=geo.local_tz(tz)).strftime("%H:%M") if tst else ""
    return f"📍 {rem['text']}\n({who} {verb} {place_label(rem['place'])}{', ' + when if when else ''})"


def checker_health(data: dict[str, Any], now_ts: float) -> dict[str, Any]:
    last = data.get("checker", {}).get("last_run")
    if last is None:
        return {"checker_running": False, "checker_note": "The reminder checker has never run."}
    age = now_ts - last
    out: dict[str, Any] = {"checker_running": age <= CHECKER_STALE_S, "checker_last_run": geo.humanize_age(age)}
    if age > CHECKER_STALE_S:
        out["checker_note"] = "The reminder checker has not run recently."
    return out
