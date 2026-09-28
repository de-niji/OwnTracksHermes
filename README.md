# OwnTracks für Hermes Agent

Verbindet [OwnTracks](https://owntracks.org) mit [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(Nous Research). Danach kann Hermes Fragen beantworten wie:

- „Wo bin ich gerade?“ / „Wo ist Anna?“
- „Wo war ich gestern Nachmittag?“ / „Wann bin ich heute im Büro angekommen?“
- „Wie weit ist Anna noch von zu Hause weg?“
- „Wie viele Kilometer bin ich diese Woche gefahren?“

Das Projekt besteht aus zwei Teilen:

1. **`owntracks-mcp`**: ein MCP-Server (Python), der die REST-API des
   [OwnTracks Recorders](https://github.com/owntracks/recorder) abfragt. Er funktioniert
   mit Hermes und mit jedem anderen MCP-Client (Claude Desktop, Cursor, …).
2. **`skills/owntracks/SKILL.md`**: ein Hermes-Skill, der dem Agenten erklärt, wann und
   wie er die Tools benutzt (Alter der Position nennen, Aufenthalte zusammenfassen,
   Datenschutz).

Alle Zugriffe sind **nur lesend**. Der Lösch-Endpunkt des Recorders (`/api/0/kill`) wird
nie aufgerufen.

```
OwnTracks-App ──MQTT/HTTP──▶ OwnTracks Recorder ◀──HTTP── owntracks-mcp ◀──MCP (stdio)── Hermes Agent
```

## Tools

| Tool (in Hermes: `mcp_owntracks_…`) | Zweck |
|---|---|
| `get_last_location(user?, device?)` | Letzte Position: Adresse, Regionen, Alter, Genauigkeit, Tempo, Akku, Kartenlink. Ohne `user` → alle. `user="me"` → Standardbenutzer. |
| `get_location_history(user, device?, start?, end?, max_points?, stay_min_minutes?)` | Verlauf eines Zeitraums: gefahrene km, Höchstgeschwindigkeit, **Aufenthalte** (wo und wie lange) und ausgedünnte Route. |
| `get_distance(lat?, lon?, user?, device?)` | Luftlinie von der letzten Position zu einem Punkt, ohne Koordinaten zu `OWNTRACKS_HOME`. |
| `list_devices()` | Alle Benutzer und Geräte. |
| `reverse_geocode(lat, lon)` | Adresse aus dem Geocache des Recorders. |
| `owntracks_status()` | Verbindungstest und aktive Konfiguration (ohne Passwort). |

Zeitangaben verstehen `now`, `today`, `yesterday`, relative Werte wie `30m`, `6h`, `2d`,
`1w` sowie `2026-09-27` oder `2026-09-27T14:30` (lokale Zeitzone).

## Voraussetzungen

- Ein laufender **OwnTracks Recorder**, den der Rechner mit Hermes per HTTP erreicht
  (Standard-Port `8083`). Falls noch keiner läuft, ist
  [owntracks/quicksetup](https://github.com/owntracks/quicksetup) der schnellste Weg.
  Alternativ: `docker run -p 8083:8083 owntracks/recorder`.
- Python ≥ 3.10 und am einfachsten [`uv`](https://docs.astral.sh/uv/).

## Installation in Hermes

### 1. MCP-Server eintragen

In `~/.hermes/config.yaml` (vollständiges Beispiel: [`examples/hermes-config.yaml`](examples/hermes-config.yaml)):

```yaml
mcp_servers:
  owntracks:                 # Name bitte so lassen, der Skill erwartet mcp_owntracks_*
    command: uvx
    args: ["--from", "git+https://github.com/de-niji/OwnTracksHermes", "owntracks-mcp"]
    env:
      OWNTRACKS_URL: "http://localhost:8083"
      OWNTRACKS_TIMEZONE: "Europe/Berlin"
      OWNTRACKS_DEFAULT_USER: "alice"
      OWNTRACKS_DEFAULT_DEVICE: "phone"
      OWNTRACKS_HOME: "51.9607,7.6261"
    timeout: 60
```

Falls das GitHub-Repo privat ist oder du lieber lokal installierst:

```bash
git clone https://github.com/de-niji/OwnTracksHermes ~/OwnTracksHermes
cd ~/OwnTracksHermes && uv venv && uv pip install .
# dann in der config.yaml:  command: /home/<du>/OwnTracksHermes/.venv/bin/owntracks-mcp
```

Passwörter gehören in `~/.hermes/.env` und werden in der Config mit `${OWNTRACKS_PASSWORD}`
referenziert.

### 2. Skill installieren

Eine der beiden Varianten reicht:

```bash
# a) kopieren
cp -r skills/owntracks ~/.hermes/skills/

# b) oder direkt aus dem geklonten Repo laden, in ~/.hermes/config.yaml:
# skills:
#   external_dirs:
#     - /home/<du>/OwnTracksHermes/skills
```

### 3. Testen

```bash
hermes mcp test owntracks      # sollte 6 Tools melden
hermes chat
> Wo bin ich gerade?
```

## Konfiguration (Umgebungsvariablen)

| Variable | Pflicht | Bedeutung |
|---|---|---|
| `OWNTRACKS_URL` | ja | Basis-URL des Recorders, z. B. `http://localhost:8083` oder `https://example.com/owntracks` (ohne `/api/0`) |
| `OWNTRACKS_USERNAME` / `OWNTRACKS_PASSWORD` | nein | HTTP-Basic-Auth, falls ein Reverse-Proxy (nginx, Caddy, Traefik) davor sitzt |
| `OWNTRACKS_VERIFY_SSL` | nein | `true` (Standard), `false`, oder Pfad zu einem CA-Bundle |
| `OWNTRACKS_TIMEZONE` | nein | z. B. `Europe/Berlin`. Standard ist die Systemzeitzone |
| `OWNTRACKS_DEFAULT_USER` | nein | Wer „ich“/`me` ist |
| `OWNTRACKS_DEFAULT_DEVICE` | nein | Standardgerät dieses Benutzers |
| `OWNTRACKS_HOME` | nein | `lat,lon` des Zuhauses für „wie weit von zu Hause“ |
| `OWNTRACKS_TIMEOUT` | nein | HTTP-Timeout in Sekunden (Standard 15) |

Adressen erscheinen nur, wenn der Recorder Reverse-Geocoding macht (`OTR_GEOKEY`).
Benannte Orte wie „Home“ oder „Work“ kommen aus den **Regionen** (Waypoints) der OwnTracks-App.

## Sicherheit

Der OwnTracks Recorder hat **keine eigene Authentifizierung**. Wer die API erreicht, kann
alle Daten lesen und sogar löschen. Deshalb:

- Den Recorder nur lokal oder im VPN erreichbar machen, oder einen Reverse-Proxy mit
  Basic-Auth davor setzen (dann `OWNTRACKS_USERNAME`/`OWNTRACKS_PASSWORD` nutzen).
- Dieser MCP-Server stellt ausschließlich lesende Tools bereit, markiert sie als
  `readOnlyHint` und ruft den Lösch-Endpunkt nie auf.
- Standortdaten sind sehr persönlich. Hermes wird über den Skill angewiesen, sie nur an
  die fragende Person herauszugeben.

## Als HTTP-Server betreiben (optional)

Wenn Hermes auf einem anderen Rechner läuft als der Recorder:

```bash
owntracks-mcp --transport streamable-http --host 0.0.0.0 --port 8765
```

```yaml
mcp_servers:
  owntracks:
    url: "http://recorder-host:8765/mcp"
```

Diesen Port nicht ungeschützt ins Internet stellen.

## Entwicklung

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
```

Die Tests laufen gegen einen nachgebauten Recorder (`tests/fake_recorder.py`). Einer davon
startet den echten Server per stdio, so wie Hermes es tut. Getestet mit MCP-Python-SDK 1.x
und 2.x.

## Lizenz

MIT
