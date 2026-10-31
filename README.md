# Kotodama 言霊

Server for the Hermes smart speakers. It covers:

- **Adoption.** A new speaker shows a 3-digit code on its screen. You type the code into the admin page to pair it.
- **Configuration.** Per speaker: character, screen layout, colour scheme, brightness, volume, wake word, Wi-Fi
  networks, time zone, and the Hermes URL. Changes reach the speaker straight away over its WebSocket.
- **Animations.** Characters are stored here and resized to the exact pixel size each layout needs.
- **Live status, logs and recordings** from every speaker.

The device side of the contract is in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Run locally

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
set KOTODAMA_PUBLIC_URL=http://localhost:8000
.venv\Scripts\uvicorn --factory app.main:create_app --port 8000
```

Then open http://localhost:8000/admin. The first visit asks you to choose an admin password. Data goes to `./data` unless `KOTODAMA_DATA_DIR` is set.

To try it without hardware, run a fake speaker. It prints the adoption code to enter on the admin page:

```
.venv\Scripts\python tools\simulate_device.py --url http://localhost:8000
```

Run the tests with `.venv\Scripts\pytest -q`.

## Settings

| Variable | Default | |
|---|---|---|
| `KOTODAMA_ADMIN_PASSWORD` | unset | Normally leave this unset. The first visit to `/admin` asks you to choose a password, which is stored hashed in the database, and **Password** in the admin UI changes it. Set this variable only to get back in after losing that password: it is used only while no UI password exists, so delete `admin_password_hash` from the `settings` table first. |
| `KOTODAMA_SESSION_SECRET` | generated and stored in the database | Signs admin session cookies |
| `KOTODAMA_PUBLIC_URL` | `https://kotodama.mylife.ninja` | Base URL put into the asset links sent to speakers |
| `KOTODAMA_DATA_DIR` | `./data` (`/data` in the image) | SQLite database, characters, recordings |
| `KOTODAMA_SECURE_COOKIES` | `true` for an https public URL | |
| `KOTODAMA_ADOPTION_TTL_S` | `600` | How long an adoption code stays valid |
| `KOTODAMA_LOG_RETENTION` | `5000` | Log lines kept per speaker |
| `KOTODAMA_MAX_RECORDING_BYTES` | 10 MB | |

## Deploy

Jenkins job **Build kotodama image** (definition in [jenkins/build-image.xml](jenkins/build-image.xml)) polls this
repo every 5 minutes, runs the tests, and pushes `registry.gitlab.com/ninjalife/kotodama:latest` and `:<sha>`. The Kubernetes manifest
is `playbooks/manifests/kotodama-speaker.yml` in `kubernetes-deployments`. The project is public, so the cluster pulls
the image without credentials.

Run it as **one replica**. Speaker connections are held in memory, and SQLite has a single writer.

## Layout

```
app/
  main.py        app factory
  device_api.py  speaker endpoints: ping, adoption, WebSocket, config, status, logs, recordings, assets
  admin.py       admin pages and JSON API (session login, CSRF header check)
  models.py      config schema and request bodies (pydantic)
  themes.py      colour schemes and screen layouts
  assets.py      character store: GIF normalization and per-size variants
  starter.py     draws the built-in "default" character
  hub.py         live WebSocket connections and the admin event stream (SSE)
  db.py          SQLite
tools/simulate_device.py   fake speaker for testing
```
