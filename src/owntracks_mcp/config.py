"""Configuration, read from environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import tzinfo
from zoneinfo import ZoneInfo


class ConfigError(ValueError):
    pass


_UNRESOLVED = re.compile(r"^\$\{[^}]*\}$")


def _clean_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop empty values and unresolved ``${VAR}`` placeholders.

    Hermes keeps a ``${VAR}`` reference literally when the variable is not set,
    which would otherwise be sent as a user name or password.
    """
    return {k: v for k, v in env.items() if v.strip() and not _UNRESOLVED.match(v.strip())}


def recorder_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The OWNTRACKS_* settings that are actually set."""
    cleaned = _clean_env(os.environ if env is None else env)
    return {k: v for k, v in cleaned.items() if k.startswith("OWNTRACKS_")}


def _parse_bool_or_path(value: str | None) -> bool | str:
    if value is None or value.strip() == "":
        return True
    v = value.strip()
    if v.lower() in ("1", "true", "yes", "on"):
        return True
    if v.lower() in ("0", "false", "no", "off"):
        return False
    return v  # path to a CA bundle


def _parse_point(value: str | None) -> tuple[float, float] | None:
    if not value or not value.strip():
        return None
    try:
        lat_s, lon_s = value.split(",", 1)
        lat, lon = float(lat_s), float(lon_s)
    except ValueError as exc:
        raise ConfigError(f"OWNTRACKS_HOME must look like '51.96,7.62', got {value!r}") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ConfigError(f"OWNTRACKS_HOME is out of range: {value!r}")
    return lat, lon


@dataclass(frozen=True)
class Settings:
    url: str
    username: str | None = None
    password: str | None = None
    verify: bool | str = True
    timeout: float = 15.0
    timezone: tzinfo | None = None
    default_user: str | None = None
    default_device: str | None = None
    home: tuple[float, float] | None = None

    @property
    def api_base(self) -> str:
        return f"{self.url}/api/0"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = _clean_env(os.environ if env is None else env)

        url = (env.get("OWNTRACKS_URL") or "").strip().rstrip("/")
        if not url:
            raise ConfigError(
                "OWNTRACKS_URL is not set. Point it at your OwnTracks Recorder, "
                "e.g. http://localhost:8083"
            )
        if url.endswith("/api/0"):
            url = url[: -len("/api/0")]

        tz_name = (env.get("OWNTRACKS_TIMEZONE") or "").strip()
        try:
            tz = ZoneInfo(tz_name) if tz_name else None
        except Exception as exc:  # ZoneInfoNotFoundError, ValueError
            raise ConfigError(f"Unknown OWNTRACKS_TIMEZONE {tz_name!r}") from exc

        try:
            timeout = float(env.get("OWNTRACKS_TIMEOUT") or 15)
        except ValueError as exc:
            raise ConfigError("OWNTRACKS_TIMEOUT must be a number of seconds") from exc

        return cls(
            url=url,
            username=env.get("OWNTRACKS_USERNAME") or None,
            password=env.get("OWNTRACKS_PASSWORD") or None,
            verify=_parse_bool_or_path(env.get("OWNTRACKS_VERIFY_SSL")),
            timeout=timeout,
            timezone=tz,
            default_user=env.get("OWNTRACKS_DEFAULT_USER") or None,
            default_device=env.get("OWNTRACKS_DEFAULT_DEVICE") or None,
            home=_parse_point(env.get("OWNTRACKS_HOME")),
        )
