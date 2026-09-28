"""A tiny in-memory stand-in for the OwnTracks Recorder HTTP API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
T0 = int(datetime(2026, 9, 28, 6, 0, 0, tzinfo=timezone.utc).timestamp())  # 08:00 Berlin

HOME = (51.960700, 7.626100)
OFFICE = (51.950000, 7.580000)  # ~3.4 km west of HOME


def _rec(tst: int, lat: float, lon: float, user: str, device: str, **extra: Any) -> dict[str, Any]:
    return {
        "_type": "location",
        "tst": tst,
        "lat": lat,
        "lon": lon,
        "acc": 12,
        "batt": 80,
        "tid": device[:2],
        "topic": f"owntracks/{user}/{device}",
        **extra,
    }


def nico_track() -> list[dict[str, Any]]:
    pts = []
    # 08:00-09:00 at home, every 5 minutes
    for i in range(13):
        pts.append(_rec(T0 + i * 300, HOME[0] + 0.0001 * (i % 2), HOME[1], "nico", "pixel",
                        addr="Prinzipalmarkt 1, Münster", inregions=["Home"]))
    # 09:00-09:15 drive, one point per minute
    for i in range(1, 15):
        f = i / 15
        pts.append(_rec(T0 + 3600 + i * 60,
                        HOME[0] + (OFFICE[0] - HOME[0]) * f,
                        HOME[1] + (OFFICE[1] - HOME[1]) * f,
                        "nico", "pixel", vel=35))
    # 09:15-11:00 at office, every 5 minutes
    for i in range(22):
        pts.append(_rec(T0 + 4500 + i * 300, OFFICE[0], OFFICE[1], "nico", "pixel",
                        addr="Bürostraße 5, Münster", inregions=["Work"]))
    return pts


class FakeRecorder:
    def __init__(self) -> None:
        self.devices = {"nico": ["pixel"], "anna": ["iphone", "ipad"]}
        self.tracks = {("nico", "pixel"): nico_track()}
        self.last = {
            ("nico", "pixel"): self.tracks[("nico", "pixel")][-1],
            ("anna", "iphone"): _rec(int(NOW.timestamp()) - 120, 48.137, 11.575, "anna", "iphone",
                                     addr="Marienplatz, München", cc="DE", conn="m", vel=0, bs=2),
            ("anna", "ipad"): _rec(int(NOW.timestamp()) - 86400 * 3, 48.140, 11.580, "anna", "ipad"),
        }
        self.geocache = {(51.9607, 7.6261): {"addr": "Prinzipalmarkt 1, Münster", "cc": "DE", "tst": 1}}
        self.requests: list[tuple[str, dict[str, str]]] = []

    def handle(self, path: str, query: str) -> tuple[int, Any]:
        """Return (status, json-able body) for a GET request."""
        q = {k: v[0] for k, v in parse_qs(query).items()}
        verb = path.rsplit("/api/0/", 1)[-1]
        self.requests.append((verb, q))
        user, device = q.get("user"), q.get("device")

        if verb == "version":
            return 200, {"version": "0.9.9-fake"}
        if verb == "list":
            if user:
                return 200, {"results": self.devices.get(user, [])}
            return 200, {"results": list(self.devices)}
        if verb == "last":
            recs = [
                r for (u, d), r in self.last.items()
                if (not user or u == user) and (not device or d == device)
            ]
            return 200, recs
        if verb == "locations":
            if not user or not device:
                return 416, {"error": "user and device required"}
            frm = _from_rec_time(q["from"])
            to = _from_rec_time(q["to"])
            data = [r for r in self.tracks.get((user, device), []) if frm <= r["tst"] <= to]
            return 200, {"count": len(data), "data": data, "status": 200}
        if verb == "q":
            key = (round(float(q["lat"]), 4), round(float(q["lon"]), 4))
            return 200, self.geocache.get(key, {})
        return 404, {"error": "unknown"}


def _from_rec_time(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def as_httpx_handler(fake: FakeRecorder):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = fake.handle(request.url.path, request.url.query.decode())
        return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    return handler


def serve_http(fake: FakeRecorder, username: str | None = None, password: str | None = None):
    """Start a real HTTP server in a thread; returns (server, base_url)."""
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    expected = None
    if username:
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if expected and self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.end_headers()
                return
            u = urlparse(self.path)
            status, body = fake.handle(u.path, u.query)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # silence
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
