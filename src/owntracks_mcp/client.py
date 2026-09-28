"""Thin async client for the OwnTracks Recorder HTTP API (/api/0/...).

Only read endpoints are implemented on purpose; ``/api/0/kill`` (which deletes
data) is never called.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class RecorderError(RuntimeError):
    pass


class RecorderClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        auth = None
        if settings.username:
            auth = httpx.BasicAuth(settings.username, settings.password or "")
        self._http = httpx.AsyncClient(
            base_url=settings.api_base,
            auth=auth,
            verify=settings.verify,
            timeout=settings.timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "owntracks-mcp"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, verb: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.get(f"/{verb}", params=clean)
        except httpx.HTTPError as exc:
            raise RecorderError(
                f"Could not reach the OwnTracks Recorder at {self.settings.url}: {exc}"
            ) from exc
        if resp.status_code in (401, 403):
            raise RecorderError(
                f"The Recorder refused access (HTTP {resp.status_code}). "
                "Check OWNTRACKS_USERNAME / OWNTRACKS_PASSWORD."
            )
        if resp.status_code >= 400:
            raise RecorderError(f"Recorder returned HTTP {resp.status_code} for /api/0/{verb}: {resp.text[:200]}")
        if not resp.content.strip():
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise RecorderError(
                f"Recorder returned non-JSON for /api/0/{verb} (is OWNTRACKS_URL correct?): {resp.text[:200]}"
            ) from exc

    async def version(self) -> str | None:
        data = await self._get("version")
        return data.get("version") if isinstance(data, dict) else None

    async def list_users(self) -> list[str]:
        return _results(await self._get("list"))

    async def list_devices(self, user: str) -> list[str]:
        return _results(await self._get("list", {"user": user}))

    async def last(self, user: str | None = None, device: str | None = None) -> list[dict[str, Any]]:
        data = await self._get("last", {"user": user, "device": device})
        if isinstance(data, dict):
            data = data.get("data", data.get("results", [data]))
        return [r for r in (data or []) if isinstance(r, dict)]

    async def locations(
        self, user: str, device: str, *, start: str, end: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        data = await self._get(
            "locations",
            {"user": user, "device": device, "from": start, "to": end, "limit": limit, "format": "json"},
        )
        if isinstance(data, dict):
            data = data.get("data", [])
        return [r for r in (data or []) if isinstance(r, dict)]

    async def reverse_geocode(self, lat: float, lon: float) -> dict[str, Any] | None:
        data = await self._get("q", {"lat": lat, "lon": lon})
        if isinstance(data, dict) and (data.get("addr") or data.get("cc")):
            return data
        return None


def _results(data: Any) -> list[str]:
    if isinstance(data, dict):
        data = data.get("results", [])
    return [str(x) for x in (data or [])]
