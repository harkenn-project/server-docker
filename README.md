# Harkenn

*Harken: to listen.* Harkenn is a private voice assistant for your home. Small speakers listen and answer; this
server does the thinking, on your own hardware. Nothing goes to the cloud unless you point it at a cloud assistant.

<p align="center">
  <a href="docs/media/demo.mp4"><img src="docs/media/demo-poster.jpg" width="240"
    alt="A Harkenn speaker on a shelf, its round screen showing the Kotodama character listening (click to play the video)"></a>
  <br><em>A speaker introducing itself (1:39, with sound): wake word, listening, thinking, answering.</em>
</p>

- **Talk to it.** Say the wake word (Okay Nabu, Hey Jarvis, Hey Mycroft, Alexa, or your own model), or tap the
  speaker, and ask. The wake word is heard on the speaker itself: nothing leaves it until then. Speech recognition
  (Whisper) and speech (Piper) run inside the server, which hears when you've finished talking.
- **Your choice of assistant.** Any OpenAI-compatible API (OpenAI, DeepSeek, OpenRouter, Gemini, Ollama, LM Studio,
  ...), Anthropic, or a [Hermes Agent](https://hermes-agent.nousresearch.com) with its own memory and tools. Without
  one, a few built-in replies.
- **Conversations.** When the assistant asks you something, the speaker listens for your answer without the wake
  word. The assistant sees only your last few conversations from the last hour (both adjustable), so answers stay
  quick and cheap.
- **Agents can talk back.** A built-in [MCP server](docs/MCP.md) lets an AI agent announce things on the speakers,
  or ring them like a phone and talk to whoever answers. Each speaker can be set to *speak only* or *do not
  disturb*, with quiet hours.
- **Knows who's talking (opt-in).** With each person's agreement, Harkenn learns their voice on the **People** page
  and tells the assistant who is probably speaking, and from which speaker, with how to say their name and their
  gender identity if you give them. It's a hint for personal answers, never a lock: the assistant is told it can be
  wrong or fooled.
- **Shows as well as tells.** With an answer, the assistant can put a picture, chart or buttons on the speaker's
  screen, and tapping a button answers it ([how](docs/SCREEN.md)).
- **Home Assistant.** Every speaker shows up as a device: what it's doing as it happens, its volume, a notifier for
  announcements, and more, through MQTT ([how](docs/HOMEASSISTANT.md)).
- **Only the nearest answers.** With several speakers in earshot, the one that heard the wake word best answers: the
  speakers tell each other how loudly they heard it with short tones too high for people to hear, and the rest stay
  quiet.
- **Animated characters** on the speaker's round screen: listening, thinking, talking, sleeping, muted, ringing.
  Four come built in (Kotodama, Obake, Neko and Robo) and you can upload your own. While a speaker sleeps, its screen
  shows the character sleeping, only a big clock, or nothing: your choice per speaker.
- **Sound packs.** Choose what each speaker plays when it starts, wakes, finishes listening, hits an error and shuts
  down: seven built-in packs, voiced packs (a line for each sound, in any Piper voice) or your own uploaded clips.
- **Setup without fuss.** Install the firmware from your browser at [flash.mylife.ninja](https://flash.mylife.ninja).
  Speakers then find the server on the network, show a 3-digit code, and you enter it in the admin page to adopt
  them. Screen layout, colours, volume, Wi-Fi and voice are all set from the admin page.
- **Updates over Wi-Fi.** Speakers check for new firmware when they start and offer to install it. The server
  follows a release channel of your choice: stable, beta, alpha or development.
- **Break glass.** Every speaker has its own web page, on its network or on its own hotspot when it can't join its
  Wi-Fi, for fixing Wi-Fi, static IP, the server address, logs and firmware. It takes the admin password or a speaker
  password made at adoption, and locks itself after too many wrong ones. Changes made there come back to Harkenn.
  Speakers can also be factory reset from the admin page. See [PROTOCOL.md](docs/PROTOCOL.md#8-the-speakers-own-web-page-break-glass).
- **Away from home.** A speaker set up for remote connection keeps working when it's taken somewhere it can't reach
  the server: it goes through a relay, end-to-end encrypted so the relay can't listen in, shows a remote icon, and
  comes back by itself. The admin page warns you while it does ([how](docs/RELAY.md)).

## Get a speaker

Harkenn runs on the **Spotpear ESP32-S3 1.28" BOX**: a small cylinder with a round 1.28" touch screen, a microphone,
a speaker and a battery (CA$41 when we bought ours). On AliExpress it's sold as
[*Superior-ESP32 S3 1.28 Inch LCD Touch Screen Mini Speaker ESP32 S3 Xiaozhi AI Mini Speaker Support Deepseek*](https://www.aliexpress.com/item/1005010093339429.html).
It ships with XiaoZhi firmware, which you replace with Harkenn's over USB the first time: from Chrome or Edge at
[flash.mylife.ninja](https://flash.mylife.ninja), which checks it's the right board first
([harkenn-project/flash](https://github.com/harkenn-project/flash)), or by building it yourself
([speaker-firmware](https://github.com/harkenn-project/speaker-firmware) has the steps). Similar-looking Spotpear boxes
(a 1.85" round screen, or no screen) aren't supported, so check it's the 1.28" one.

## Screenshots

The admin pages, from a demo server with made-up speakers and people.

| | |
|---|---|
| [<img src="docs/media/admin-speakers.png" alt="The Speakers page: three speakers online, with Wi-Fi signal, battery and firmware">](docs/media/admin-speakers.png) **Speakers** at a glance | [<img src="docs/media/admin-speaker.png" alt="A speaker's page: live status and its configuration, with a preview of its screen">](docs/media/admin-speaker.png) **A speaker**: live status, screen, sound and wake word |
| [<img src="docs/media/admin-voice.png" alt="The Voice page: speech recognition, voice and assistant settings">](docs/media/admin-voice.png) **Voice**: speech, the voice, and your assistant | [<img src="docs/media/admin-screen.png" alt="The Screen page: a preview of a screen asking 'Turn on the porch light?' with Yes and No buttons">](docs/media/admin-screen.png) **Screen**: try what the assistant can show |
| [<img src="docs/media/admin-characters.png" alt="The Characters page: the built-in Kotodama and Obake characters in every state">](docs/media/admin-characters.png) **Characters**, built in or your own | [<img src="docs/media/admin-people.png" alt="The People page: a person with a name, phonetic spelling, gender identity and voice samples">](docs/media/admin-people.png) **People** and their voiceprints (opt-in) |

## Docs

| | |
|---|---|
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | How speakers and the server talk |
| [docs/MCP.md](docs/MCP.md) | The MCP server for AI agents, with a ready-made [skill](docs/skills/harkenn-speakers/SKILL.md) |
| [docs/SCREEN.md](docs/SCREEN.md) | Pictures, buttons and overlays the assistant can show |
| [docs/HOMEASSISTANT.md](docs/HOMEASSISTANT.md) | The speakers in Home Assistant, through MQTT |
| [docs/RELAY.md](docs/RELAY.md) | Speakers away from home, through the relay ([harkenn-project/flash](https://github.com/harkenn-project/flash), also the web flasher) |

## Run it

```
docker compose up -d
```

with [docker-compose.yml](docker-compose.yml), then open `http://<this machine>:8000/admin` and choose an admin
password. The first answer takes a while: the voice models download into the data volume the first time.

The server announces itself on the network as `harkenn.local`, so speakers on the same network find it on their own.
If they can't (another VLAN, or a Kubernetes cluster), add a DNS record **`harkenn`** pointing at the server in your
router, or type the address on the speaker's own page (its hotspot `Harkenn-XXXX`, at 192.168.4.1), under
*Settings*.

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
| `HARKENN_UPDATE_CHANNEL` | `stable` | `stable`, `beta`, `alpha` or `development`. Can be changed on the **Firmware** page |
| `HARKENN_ADOPTION_TTL_S` | `600` | How long an adoption code stays valid |
| `HARKENN_LOG_RETENTION` | `5000` | Log lines kept per speaker |
| `HARKENN_RECORDINGS_PER_DEVICE` | `200` | Recordings kept per speaker (when keeping recordings is on); the oldest go first |
| `HARKENN_FIRMWARE_KEPT` | `10` | Firmware images kept on the Firmware page, uploads and downloaded releases together; the oldest go first |
| `HARKENN_RELAY_URL` | unset | The relay for speakers away from home, as speakers reach it (e.g. `wss://flash.mylife.ninja`). Unset: no remote connection. See [RELAY.md](docs/RELAY.md) |
| `HARKENN_RELAY_SERVER_URL` | `HARKENN_RELAY_URL` | The relay as this server reaches it, if different (e.g. inside a cluster) |
| `HARKENN_ASSISTANT_API` | `openai` | `openai`, `anthropic` or `hermes`: used with `HARKENN_ASSISTANT_URL` |
| `HARKENN_ASSISTANT_URL`, `HARKENN_ASSISTANT_MODEL`, `HARKENN_ASSISTANT_KEY` | unset | The assistant, e.g. from a secret. The **Voice** page's own values win; these fill in what it leaves empty |
| `HARKENN_MCP_TOKEN` | unset | An extra access token for the MCP server, besides those made on the **Agents** page |
| `HARKENN_MQTT_URL` | unset | Home Assistant's MQTT broker, `mqtt://user:password@host:1883`, until the **Server** page sets one |

Everything else is in the admin UI: speakers on **Speakers** (and **Defaults** for new ones), the assistant, speech
and wake words on **Voice**, voiceprints on **People**, the assistant's screens on **Screen**, MCP tokens on
**Agents**, firmware and its release channel on **Firmware**, and the public address and Home Assistant on
**Server**.

Run **one replica**: speaker connections are held in memory, and SQLite has a single writer.

## Build

A Jenkins job ([jenkins/build-image.xml](jenkins/build-image.xml)) polls this repo, runs the tests, and pushes
`ghcr.io/harkenn-project/server-docker:latest` and `:<sha>`. The image bundles the wake word models and the
built-in characters, and cairo with DejaVu fonts for SVG screens (without cairo, SVG screens are turned off and the
rest works).

## Layout

```
app/
  main.py        app factory
  device_api.py  speaker endpoints: ping, adoption, WebSocket, config, status, logs, firmware, assets
  admin.py       admin pages and JSON API (session login, CSRF header check)
  local_access.py  passwords for the speakers' own web pages (hashes, hotspot key, unlock)
  relay.py       speakers away from home, through the relay (docs/RELAY.md)
  voice/         speech recognition, end of speech (vad.py), replies (brain.py), speech, ringing (calls.py),
                 voiceprints (voiceprint.py)
  mcp_server.py  the MCP server for agents (docs/MCP.md, docs/skills/)
  screens.py     the assistant's screens: drawing pictures, SVG, buttons and overlays (docs/SCREEN.md)
  homeassistant.py  the speakers in Home Assistant (docs/HOMEASSISTANT.md); mqtt.py a small MQTT client
  sounds.py      sound packs; wakewords.py wake word models
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

Created by **Master Magic** and **Pip**, Master Magic's AI apprentice, with the Harkenn contributors. Pip is the name we give the AI assistant that co-writes Harkenn.

## Support

Buy us a coffee on [GitHub Sponsors](https://github.com/sponsors/harkenn-project). Half of every donation goes to the
open-source projects Harkenn is built on: see [SUPPORT.md](SUPPORT.md) for the shares, and [THANKS.md](THANKS.md) for
everyone who has helped and every payout made.

## Licence

[MIT](LICENSE), copyright the Harkenn contributors. Third-party components keep their own licences (see
[CREDITS.md](CREDITS.md)). Note that Piper, the built-in speech engine, is GPL-3.0: the Docker image, which includes
it, is distributed under the GPL's terms as a whole. Piper voices each have their own licence; the default voice,
LJ Speech, is public domain.
