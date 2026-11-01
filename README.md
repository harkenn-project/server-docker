# Harkenn

*Harken: to listen.* Harkenn is a private voice assistant for your home. Small speakers listen and answer; this
server does the thinking, on your own hardware. Nothing goes to the cloud unless you point it at a cloud assistant.

- **Talk to it.** Tap a speaker (wake words are coming) and ask. Speech recognition (Whisper) and speech (Piper)
  run inside the server; answers come from any OpenAI-compatible assistant you choose (Ollama, llama.cpp, LM Studio,
  a cloud API), or from a few built-in replies when there isn't one.
- **Animated characters** on the speaker's round screen: listening, thinking, talking, sleeping, muted. Four come
  built in (Kotodama, Obake, Neko and Robo) and you can upload your own.
- **Setup without fuss.** Speakers find the server on the network, show a 3-digit code, and you enter it in the admin
  page to adopt them. Screen layout, colours, volume, Wi-Fi and voice are all set from the admin page.
- **Updates over Wi-Fi.** Speakers check for new firmware when they start and offer to install it.

The speaker firmware is in [speaker-firmware](https://github.com/harkenn-project/speaker-firmware). The
contract between them is [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Run it

```
docker compose up -d
```

with [docker-compose.yml](docker-compose.yml), then open `http://<this machine>:8000/admin` and choose an admin
password. The first answer takes a while: the voice models download into the data volume the first time.

The server announces itself on the network as `harkenn.local`, so speakers on the same network find it on their own.
If they can't (another VLAN, or a Kubernetes cluster), add a DNS record **`harkenn`** pointing at the server in your
router, or type the address into the speaker's Wi-Fi setup page under *Advanced*.

## Develop

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
set HARKENN_PUBLIC_URL=http://localhost:8000
.venv\Scripts\uvicorn --factory app.main:create_app --port 8000
.venv\Scripts\pytest -q
```

To try it without hardware, `tools\simulate_device.py --url http://localhost:8000` runs a fake speaker that prints
its adoption code.

## Settings

Environment variables.

| Variable | Default | |
|---|---|---|
| `HARKENN_PUBLIC_URL` | `http://harkenn.local:8000` | The server's public address. Can be changed on the admin UI's **Server** page |
| `HARKENN_DATA_DIR` | `./data` (`/data` in the image) | Database, characters, recordings, voice models, firmware |
| `HARKENN_ADMIN_PASSWORD` | unset | Recovery only. The admin password is set in the web UI; this applies only while none is stored, so delete `admin_password_hash` from the `settings` table first |
| `HARKENN_SESSION_SECRET` | generated and stored | Signs admin session cookies |
| `HARKENN_SECURE_COOKIES` | `true` for an https public URL | |
| `HARKENN_MDNS` | `true` | Announce `_harkenn._tcp` / `harkenn.local` on the network |
| `HARKENN_VOICE_WARMUP` | `true` | Load the voice models at startup |
| `HARKENN_WHISPER_THREADS` | up to 8 | CPU threads for speech recognition |
| `HARKENN_UPDATE_URL` | the `releases` branch of speaker-firmware, raw | Where firmware releases (`latest.json`) are read from; empty turns update checks off |
| `HARKENN_ADOPTION_TTL_S` | `600` | How long an adoption code stays valid |
| `HARKENN_LOG_RETENTION` | `5000` | Log lines kept per speaker |

Voice settings (speech recognition model, voice, assistant) are on the admin UI's **Voice** page.

Run **one replica**: speaker connections are held in memory, and SQLite has a single writer.

## Build

Jenkins job **Build harkenn server image** ([jenkins/build-image.xml](jenkins/build-image.xml)) polls this repo, runs
the tests, and pushes `ghcr.io/harkenn-project/server-docker:latest` and `:<sha>`.

## Layout

```
app/
  main.py        app factory
  device_api.py  speaker endpoints: ping, adoption, WebSocket, config, status, logs, firmware, assets
  admin.py       admin pages and JSON API (session login, CSRF header check)
  voice/         speech recognition, replies and speech (Whisper, Piper, Wyoming, OpenAI-compatible APIs)
  updates.py     firmware releases from Git; firmware.py checks and stores images
  discovery.py   mDNS announcement
  assets.py      character store; kani.py the speakers' animation format; starter.py draws the built-in characters
  models.py      config schema and request bodies (pydantic)
  themes.py      colour schemes and screen layouts
  hub.py         live WebSocket connections and the admin event stream (SSE)
  db.py          SQLite
tools/simulate_device.py   fake speaker for testing
```

## Name

Harkenn means *the one that listens* (from *harken*). It began as a homelab project called Kotodama (言霊, "word
spirit"), which lives on as the default character.

## Credits

Harkenn is built on Whisper, Piper and many other open-source projects: see [CREDITS.md](CREDITS.md).

<img src="docs/pip.png" alt="Pip, a round blue character in a wizard hat holding a wand" width="96" align="right">

Created by **Master Magic** and **Pip**, his AI apprentice, with the Harkenn contributors. Pip is the name we give the AI assistant that co-writes Harkenn.

## Support

Buy us a coffee on [GitHub Sponsors](https://github.com/sponsors/harkenn-project). Half of every donation goes to the
open-source projects Harkenn is built on: see [SUPPORT.md](SUPPORT.md) for the shares, and [THANKS.md](THANKS.md) for
everyone who has helped and every payout made.

## Licence

[MIT](LICENSE), copyright the Harkenn contributors. Third-party components keep their own licences (see
[CREDITS.md](CREDITS.md)). Note that Piper, the built-in speech engine, is GPL-3.0: the Docker image, which includes
it, is distributed under the GPL's terms as a whole. Piper voices each have their own licence; the default voice,
LJ Speech, is public domain.
