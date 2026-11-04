# Harkenn device protocol (v1)

This is the contract between the speaker firmware and the Harkenn server. The speaker knows nothing else: it finds
the server, and everything else comes from there. All paths are relative to the server URL. JSON everywhere unless
noted.

## 0. Finding the server

The speaker tries these in order and uses the first that answers `/api/v1/ping`:

1. The URL saved on the speaker: entered on its own page (§8) under *Settings*, or sent by the server as
   `server_url` in a config it has applied (so a server that moves takes its speakers along).
2. mDNS service `_harkenn._tcp`: `http://<address>:<port>`, or the `url` in its TXT record.
3. `http://harkenn.local:8000` (the server answers to that mDNS host name).
4. `http://harkenn` and `http://harkenn:8000`: a DNS name you can point at the server when mDNS doesn't reach the
   speakers.

## 1. Boot: wait for the server

```
GET /api/v1/ping
200 {"ok": true, "server": "harkenn", "version": "0.1.0", "time": 1793430000.0}
```

The device retries every few seconds until it gets a 200. It shows its built-in "searching" screen while it waits,
because no character animations have been downloaded yet. `time` can be used to set the clock before SNTP is up.

## 2. Adoption (first boot, or after a factory reset)

The device ID is the Wi-Fi MAC address, written as 12 lowercase hex digits (`907069f466b0`).

```
POST /api/v1/adoption/request
{"device_id": "907069f466b0", "hardware": "sp-esp32-s3-1.28-box", "firmware": "0.1.0"}

200 {"request_id": "…", "code": "482", "expires_in": 600, "poll_interval_s": 3}
```

The device shows `code` on its screen. An admin opens the Harkenn admin page, finds the waiting speaker and types the
code. The admin page never displays the code, so whoever adopts the speaker must be able to see its screen.
After 5 wrong codes the request is rejected.

If the device asks again while it already has a request waiting (for example after a reboot), it gets the same request
and the same code back.

The device polls:

```
GET /api/v1/adoption/{request_id}
200 {"state": "pending"}
200 {"state": "approved", "token": "…", "device_id": "907069f466b0"}   # returned exactly once
200 {"state": "collected" | "rejected" | "expired"}
404 unknown request
```

On `approved` the device saves `token` to NVS. On `rejected`, `expired` or 404 it starts a new request.

## 3. Authenticated calls

Every call below sends `Authorization: Bearer <token>`. A 401, or a WebSocket close with code 4401, means the
device has been forgotten. The device then erases its token and goes back to step 2.

### WebSocket (primary channel)

```
GET /api/v1/device/ws     (WebSocket upgrade, Authorization header required)
```

As soon as the socket opens, the server sends a full `config` message. It sends another one every time the config
changes.

**Server → device**

| type | fields | meaning |
|---|---|---|
| `config` | see §4 | Apply this configuration and save it as the last-known-good config |
| `command` | `command`, `args` | `reboot`, `identify` (flash the screen/LED), `refresh_config`, `set_volume` {volume}, `play_state` {state, seconds}, `update_firmware` {url, version, size, sha256}, `factory_reset` (forget everything and start over in Wi-Fi setup; the server forgets the speaker too), `card_rescan` (look for the SD card again, §10) |
| `local_changes_ack` | `id` | The speaker's `local_changes` with this id are in the stored config now (§8) |
| `update_available` | `firmware: {url, version, size, sha256}` | Reply to `update_check` when a newer release exists. The speaker offers it on screen (Install / Dismiss) |
| `ping` | | Reply with `{"type":"pong"}`. Sent every 25 s |

**Device → server**

| type | fields | meaning |
|---|---|---|
| `status` | `status: {state, firmware, ip, ssid, rssi, uptime_s, free_heap, free_psram, battery, charging, volume, muted, brightness, config_version, message, local_failures, local_locked, hotspot}` | Send on every state change and at least every 30 s. Every field is optional. The last three are about the speaker's own web page (§8) |
| `local_changes` | `id`, `changes: {wifi, wifi_removed, log_level, brightness, server_url}` | Settings changed on the speaker's own web page (§8). Sent at every connection until acknowledged |
| `log` | `lines: [{ts, level, tag, message}]` | Batches of up to 200 lines. `level` is one of E/W/I/D/V. `ts` is Unix seconds, or 0 if the clock isn't set |
| `event` | `name`, `data` | Notable events such as `wake`, `button`, `touch`, `wifi_changed` |
| `settings` | `volume` | A setting changed on the speaker itself (touch screen). The server stores it in the config without bumping the version or pushing it back |
| `update_check` | | Sent once after boot. The server checks the firmware repository and may answer `update_available` |
| `card` | `card: {present, total_mb, free_mb, music, soundscapes, meditations, photos}` | What's on the micro SD card (§10). Sent after connecting and whenever it changes |
| `config_request` | | Asks the server to send the current config again |
| `pong` | | Reply to `ping` |

### HTTP fallbacks

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/device/config` | The same payload as the WS `config` message. Returns 304 if `If-None-Match` equals the current version |
| POST | `/api/v1/device/status` | Body is the `status` object |
| POST | `/api/v1/device/logs` | Body is `{"lines": [...]}` |
| POST | `/api/v1/device/recordings?kind=utterance` | Raw body. `Content-Type` is `audio/wav`, `audio/ogg`, `audio/opus`, `audio/L16` or `application/octet-stream`. Returns 201 `{"id"}` |
| GET | `/api/v1/device/firmware/<id>.bin` | Firmware image for `update_firmware`. The device checks the size and SHA-256, writes it to the spare app slot and restarts |

## 4. Config payload

```json
{
  "type": "config",
  "version": 7,
  "device": {"id": "907069f466b0", "name": "Kitchen"},
  "config": {
    "character": "default",
    "display": {"layout": "character_clock", "theme": "midnight", "colors": {…overrides or null…},
                "brightness": 75, "sleep_after_s": 300, "clock_24h": true},
    "audio": {"volume": 70, "wake_word": "ok_nabu"},
    "wifi": [{"ssid": "Thing Tank", "password": "…", "ip": "", "subnet": "", "gateway": "", "dns": ""}],
    "timezone": "MST7MDT,M3.2.0,M11.1.0",
    "timezone_name": "America/Denver",
    "assistant": {"url": ""},
    "log_level": "info"
  },
  "theme": {"background": "#0b1020", "surface": "#1a2240", "primary": "#7aa2ff",
            "accent": "#ff7ab6", "text": "#e8ecff", "muted": "#8a93b8"},
  "layout": {"id": "character_clock", "widgets": [
      {"type": "ring", "thickness": 4},
      {"type": "character", "x": 48, "y": 22, "w": 144, "h": 144},
      {"type": "clock", "x": 40, "y": 168, "w": 160, "h": 40, "font": "lg"}]},
  "character": {"id": "default", "name": "Kotodama", "version": 3, "states": {
      "idle": {"source": "idle", "frames": 30, "duration_ms": 2400,
               "urls": {"144": "https://harkenn.example/assets/characters/default/idle.gif?size=144&v=3"}},
      "listening": {…}, "thinking": {…}, "speaking": {…}, "sleeping": {…}, "error": {…}}},
  "sounds": {"pack": "marimba", "version": 1, "slots": {
      "boot": {"url": "https://harkenn.example/assets/sounds/marimba/boot.pcm?v=1", "bytes": 41280},
      "wake": {…}, "done": {…}, "error": {"url": "", "bytes": 0}, "shutdown": {…}}},
  "wake_word": {"id": "hey_jarvis", "name": "Hey Jarvis", "url": "https://harkenn.example/assets/wakewords/hey_jarvis.tflite?v=40ff33f5",
                "bytes": 52272, "cutoff": 0.97, "window": 5, "arena": 22860},
  "local_access": {"admin_hash": "pbkdf2_sha256$10000$…$…", "speaker_hash": "pbkdf2_sha256$10000$…$…",
                   "hotspot_psk": "…64 hex digits…", "unlock": 0},
  "remote": {"url": "wss://flash.example", "id": "…32 hex digits…", "key": "…64 hex digits…"},
  "stop_word": {"url": "https://harkenn.example/assets/wakewords/stop.tflite?v=…", "bytes": 45744, "cutoff": 0.5,
                "window": 5, "arena": 21000}
}
```

What the firmware has to do with it:

* **Theme.** Use the colours as given. They already include any overrides.
* **Layout.** Widgets sit on the 240×240 round panel, (0,0) is top-left, and they are drawn in list order.
  * `ring`: an arc around the edge whose colour shows the state. idle uses `surface`, listening `primary`, thinking `muted`, speaking `accent`, error `#ff5d5d`, sleeping is hidden.
  * `character`: the animation for the current state.
  * `clock`, `date`: local time, using `timezone` and `clock_24h`. `timezone` is always a POSIX rule; when the
    zone was picked by name, `timezone_name` has it (for information) and Harkenn works out the rule from its own
    time zone database each time it sends the config.
  * `status`: one or two lines of text for the current state, a transcript, or an error.
  * Fonts: `sm` 14 px, `md` 18 px, `lg` 30 px, `xl` 48 px.
* **Character.** Every state always has an entry. A state without its own animation points at `idle` through
  `source`. The URLs are versioned, so the device can cache by URL. The files are **KANI** animations, which are
  already the size of the widget, so no scaling happens on the device. They are flattened onto the theme's
  `background` colour (`&bg=` in the URL) and are fully opaque. Assets need no token.
  * KANI format, specified in `app/kani.py`: native RGB565 pixels, frame 0 complete, later frames as
    skip/run/literal ops against the previous frame.
  * Decode into a single frame buffer. On loop, frame 0 repaints everything.
  * 240 px animations are sent at half frame rate to fit in memory.
* **Ring.** Draw the ring on top of the character, because the character's animation is opaque.
* **Wi-Fi.** `wifi` is the full list of networks the server wants the device to know. The device adds new ones to
  its saved list and updates changed ones; only its own web page removes any. `ip`, `subnet`, `gateway` and `dns`
  are a static IP for that network (empty `ip` = DHCP; firmware before 0.13 ignores them). Holding BOOT for 6 s
  always opens the hotspot and web page (§8) as a way back in.
* **Local access.** `local_access` is what the speaker's web page needs (§8). Firmware before 0.13 ignores it.
* **Remote.** `remote` is the relay and this speaker's credentials, for when it's away from home (RELAY.md); null when
  remote connection is off.
* **Stop word.** `stop_word` is the small model that hears "stop" while a timer or alarm rings (§9); null without
  one.
* **Sleeping.** After `display.sleep_after_s` without a touch the screen sleeps as `display.sleep_display` says:
  `character` (dimmed, the character's sleeping animation), `clock` (dimmed, only a big clock) or `off`. A touch,
  the wake word or anything to show wakes it.
* **Only the nearest answers.** With `audio.wake_arbitration`, speakers that hear the wake word together decide
  among themselves, without Harkenn, with *claims* above hearing (the codec runs at 48 kHz for this; everything else
  stays 16 kHz):
  * A speaker turns how loud it heard the word into a level: 0 at -10 dBFS or louder, one more per 2 dB quieter, up
    to 15. It waits 25 ms per level (plus up to 20 ms at random), then plays its claim and answers.
  * A claim is three 30 ms tones: 18.6 kHz; 19.2 kHz exactly 50 ms later (the signature, which a stray tone can't
    match); then 19.8 kHz 40 ms + 10 ms per level after that. Listeners decode the level from that gap.
  * Strongest wins. A speaker waiting to claim stays quiet if it hears a claim at its level or lower (stronger), and
    claims at once on a weaker one. A claim heard up to 1.5 s before it caught the word counts too. A speaker that
    has claimed stops answering and goes back to idle if a stronger claim comes within 1.5 s.
* **Sounds.** The sound pack's clip for each slot: `boot` (starting up), `wake` (wake word or tap), `done`
  (finished listening), `error` and `shutdown`. Clips are raw 16 kHz, 16-bit mono PCM, at most 5 s. The speaker
  keeps them in flash. `boot` plays once the first config is fully in (every animation and clip downloaded). Download only clips whose URL changed. An empty `url` is a
  silent slot. `"sounds": null` means the speaker's own built-in chimes (the Classic pack). Firmware before 0.10.0
  ignores `sounds`.
* **Shutting down.** The speaker plays `shutdown` before it restarts (an update, the reboot command) and before it
  powers off. Holding BOOT for 3 s powers it off (deep sleep); a press turns it back on.
* **Wake word.** Detected on the speaker itself, so no audio leaves it until it hears the word. `url` is a
  microWakeWord streaming model (TensorFlow Lite Micro, int8 features in, uint8 probability out). The speaker keeps it
  in flash and runs it on 40-channel features from TFLM's micro audio frontend (30 ms windows every 10 ms) while it's
  idle and unmuted, never while it's making a sound. A detection is the average of the last `window` probabilities
  reaching `cutoff`, and starts listening just like a tap. `arena` is the tensor memory the model needs.
  `"wake_word": null` means none (tap to talk). Firmware before 0.12 ignores it.
* After applying a config, the device reports `config_version` in its next `status`.

## 5. Voice

A tap (later the wake word) starts an utterance. Audio is 16 kHz, 16-bit, mono, little-endian PCM in both directions,
sent as binary WebSocket frames.

```
speaker                                     Harkenn
voice_start {session, rate, local_time}  ->
binary frames (mic, ~100 ms each)        ->           end-of-speech detection (Silero VAD)
                                         <-  listen_stop {session}   (heard speech, then audio.silence_s without it)
voice_end {session}                      ->           speech-to-text (Whisper), reply, text-to-speech (Piper)
                                         <-  voice_result {session, heard, reply}
                                         <-  speak_start {session, rate, bytes, text}
                                         <-  binary frames (speech)
                                         <-  speak_end {session}
```

* Harkenn tells speech from noise, so it decides when the speaker has finished: once it has heard speech and then
  `audio.silence_s` without any, it sends `listen_stop` and the speaker stops listening and sends `voice_end`. The
  speaker also ends the utterance itself, by loudness alone: on a tap, after `audio.silence_s` of quiet, or after 30 s.
  If neither heard speech it sends `voice_cancel` instead of `voice_end`. (Firmware before 0.8.0 ignores
  `listen_stop`.)
* `local_time` (ISO 8601, no zone) is the speaker's clock, so answers use its time zone.
* `voice_result` with an empty `heard` means nothing was understood: the speaker plays its cancel sound.
* `voice_error {session, message}` means the pipeline failed.
* Speech is sent at most about 3 s ahead of playback. `speak_start` can also arrive without an utterance (the admin
  UI's *Say it*, announcements).
* A tap while listening ends the utterance; while waiting, it sends `voice_cancel`; while speaking, it stops playback
  and sends `speak_stop`.
* `voice_wait {session}` arrives every 20 s while an answer takes long (an agent using tools). The speaker keeps
  waiting another 60 s each time.
* `speak_start {..., listen: true}`: when the speech has played, the speaker listens again, with a soft cue rather
  than the wake sound. Two cases:
  * `follow_up: true` too: the assistant answered and waits for a reply (Voice page, *listen for a reply*). The
    speaker sends `voice_start {..., follow_up: true}`, and Harkenn keeps the reply in the same conversation. If no one
    speaks, the conversation ends quietly (no cancel sound).
  * Without it: someone asked a question (an agent, through `ring`). Harkenn passes what it hears to whoever asked,
    not to the assistant.
* A **conversation** is one tap or wake word plus the replies that follow it. The assistant sees only the last few
  conversations from a recent window (Voice page; 5 from the last 60 minutes by default). Firmware before 0.11.0
  doesn't send `follow_up`, so every utterance starts a new conversation.

## 6. Ringing (agents)

An agent can ring speakers through the MCP server ([MCP.md](MCP.md)) to find out whether someone is there.

```
speaker                                     Harkenn
                                         <-  ring {ring, seconds}     to one or more speakers at once
(rings every 3 s, "Tap to answer")
ring_answer {ring}                       ->  (a tap: the first one wins)
                                         <-  ring_stop {ring}         to every speaker that rang
                                         <-  speak_start ... (the agent's message, maybe with listen: true)
```

* A speaker that's in a conversation doesn't ring; the ring just goes unanswered there.
* `ring_stop` also ends ringing on the other speakers. When `seconds` (5–30) is up, the speaker stops by itself.
* Firmware before 0.9.0 ignores `ring`, `ring_stop` and `voice_wait`.

## 7. The assistant's screen

The assistant can show something along with an answer ([SCREEN.md](SCREEN.md)). Harkenn draws the whole screen
itself, so a speaker only downloads one picture and reports taps.

```
speaker                                     Harkenn
                                         <-  screen {screen, bytes, url, regions: [{label, x, y, w, h}], dismiss, seconds}
                                         <-  binary frames: the picture (a KANI image, maybe animated), `bytes` long
                                         <-  voice_result, speak_start ...   (as usual; reply "" = only a screen)
screen_tap {session, screen, label}      ->  (a labelled area was tapped: the person's next turn)
                                         <-  voice_result {session}, speak_start ...   (the assistant's answer)
screen_dismiss {screen}                  ->  (tapped outside the areas, when dismiss is "tap")
                                         <-  screen_clear                  (hide it now)
```

* The picture comes on the socket because a speaker can't always spare the memory for a second secure connection.
  Speech and a screen's picture never interleave. `url` serves the same picture (no token needed: the id is
  unguessable and expires); firmware 0.12.0-development.5 downloads it from there instead.
* `screen` comes before the speech it goes with. The speaker shows it until a tap, `seconds` (5–600) or a new
  layout; the display stays on meanwhile. Coordinates are pixels on the 240x240 screen.
* A `screen_tap` replaces whatever was going on (listening, speaking, waiting) like a new question. A label that
  isn't on the screen the speaker was sent gets an empty `voice_result`.
* Firmware before 0.12.0 ignores these messages.

## 8. The speaker's own web page (break glass)

From firmware 0.13 every speaker serves a web page: on its network at `http://<its IP>`, and on its hotspot
`Harkenn-XXXX` (the last four hex digits of its MAC) at `http://192.168.4.1`. It shows status and logs, and lets
someone change Wi-Fi networks (with static IP), the server address, the log level, volume and brightness, upload
firmware, restart or factory reset the speaker.

* **When the hotspot comes up.** When the speaker can't join any saved network for a minute, or has none, or BOOT
  was held 6 s. The speaker doesn't go on to look for Harkenn without Wi-Fi; it keeps trying its saved networks
  every minute (not while the page was used in the last two minutes, because trying drops the hotspot's clients), and
  at once when a network is saved on the page. After BOOT, it waits 10 minutes before trying them. The hotspot goes
  off once the speaker is connected to Harkenn again.
* **Passwords.** The page takes the Harkenn admin password or the speaker's own password. A speaker that isn't adopted
  has neither, so its page is open: that's the first-time Wi-Fi setup. `local_access` in the config carries:
  * `admin_hash`, `speaker_hash`: salted PBKDF2-SHA256, `pbkdf2_sha256$<rounds>$<salt>$<digest>` (base64). The admin
    one is a separate, lighter copy that the server makes whenever the admin password is entered, so a speaker can
    check it in about a second. The speaker password is made at adoption or set in the admin UI, which can show it again (the server keeps it
    encrypted; speakers only get its hashes).
  * `hotspot_psk`: the hotspot's WPA2 key as 64 hex digits, which is PBKDF2-SHA1 of the speaker password salted with
    the hotspot's name (IEEE 802.11i). Joining the hotspot needs the speaker password; without a key it's open.
  * `unlock`: a counter. When it changes, the speaker forgets its wrong passwords.
* **Lockout.** 3 wrong passwords in a row: no password is checked for 30 s, doubling every other try (60 s after 5,
  120 s after 7, 240 s after 9). After 10 the page is locked until the admin unlocks it in Harkenn (which bumps
  `unlock`), or the speaker is factory reset with its button. A restart doesn't skip a wait. The speaker reports
  `local_failures` and `local_locked` in its status.
* **Local changes win.** Settings changed on the page are kept on the speaker and sent as `local_changes`
  `{id, changes}` at every connection, until the server answers `local_changes_ack {id}`. Until then they override the
  same settings in any config the speaker gets. `changes` may hold `wifi` (networks added or changed on the page, with
  their passwords and static IP, newest first), `wifi_removed` (names), `log_level`, `brightness` and `server_url`.
  The server merges them into the stored config, logs what changed, and pushes the new config; `server_url` is only
  logged. Volume keeps going through `settings`.

## 9. Timers, alarms and notifications

Timers and alarms live on the speaker, so they ring on time without Harkenn and survive restarts (firmware 0.13).
Harkenn sets and cancels them, and the speaker tells it what's set.

```
Harkenn → speaker  timer {action: "set", kind: "timer", seconds, label}
                   timer {action: "set", kind: "alarm", time: "07:30", days, date?, label}
                   timer {action: "cancel", what}      (an id, a label, "all", "timers" or "alarms")
                   timer {action: "show"}               (the countdown, or the alarms, on the screen)
                   timer {action: "stop"}               (stop what's ringing)
                   notify {title, text, seconds?, quiet?}
                   notifications_clear
speaker → Harkenn  timers {items: [{id, kind, label, due, remaining_s, duration?, time?, days?}], ringing?}
                   event {name: "timer_ringing" | "timer_stopped" | "timer_snoozed" | "timer_missed",
                          data: {kind, id, label, how?}}
                   notifications_seen
```

* `days` is a bit mask, bit 0 Sunday to bit 6 Saturday (`0` = once); `date` (`YYYY-MM-DD`) makes it one day. Alarms
  follow the speaker's time zone.
* `timers` comes at every connection and whenever the list changes. Harkenn trusts `remaining_s` over `due`, in
  case the speaker's clock is off.
* **Ringing** shows a shaking bell with Stop and Snooze (+1 minute for a timer, 9 for an alarm), and plays at least
  half volume. It stops on a tap, the wake word, "stop", `timer {action: "stop"}`, or after 5 minutes (timers) or
  15 minutes (alarms), which reports `timer_missed`.
* **"Stop"** is heard by a second small model, from `stop_word` in the config (Kevin Ahrendt's microWakeWord model,
  Apache-2.0), only while something rings. `stop_word` is null when the server doesn't bundle it.
* **Notifications** drop in as a card with a chime; an envelope stays in the status row at the top of the screen
  until the card is tapped (`notifications_seen`). `quiet` gives only the envelope: Harkenn sends unread ones again
  when a speaker connects.
* **The status row** at the top of every screen shows remote (through the relay), an alarm set, an unread
  notification, and the next timer's time left.

## 10. The micro SD card (firmware 0.14 and later)

The card is optional: a speaker without one works exactly the same, and Harkenn hides the media features for it.
The speaker looks for a card at boot and on `card_rescan` (the slot has no card-detect line), then sends:

```json
{"type": "card", "card": {"present": true, "total_mb": 30436, "free_mb": 29810,
  "music": [{"name": "Study", "tracks": 12}], "soundscapes": ["Rain", "White Noise"],
  "meditations": ["Breathing"], "photos": [{"name": "Sample", "pictures": 8}]}}
```

or `{"type": "card", "card": {"present": false}}`. The status carries `card: {total_mb, free_mb}` while one is in.

The layout is plain folders, so a card can be filled on any computer:

```
/Music/<album>/*.mp3|*.wav      (cover.jpg: the album's artwork)
/Soundscapes/*.wav|*.mp3        (looped; WAV loops without a gap)
/Meditations/*.mp3|*.wav
/Photos/<album>/*.jpg
```

Names are the file or folder name without the extension. Hidden files (starting with `.`) are skipped. At most 64
albums or files are listed per folder.
