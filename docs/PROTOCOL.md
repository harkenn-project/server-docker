# Harkenn device protocol (v1)

This is the contract between the speaker firmware and the Harkenn server. The speaker knows nothing else: it finds
the server, and everything else comes from there. All paths are relative to the server URL. JSON everywhere unless
noted.

## 0. Finding the server

The speaker tries these in order and uses the first that answers `/api/v1/ping`:

1. The URL saved on the speaker: entered on its Wi-Fi setup page under *Advanced*, or sent by the server as
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
| `command` | `command`, `args` | `reboot`, `identify` (flash the screen/LED), `refresh_config`, `set_volume` {volume}, `play_state` {state, seconds}, `update_firmware` {url, version, size, sha256} |
| `update_available` | `firmware: {url, version, size, sha256}` | Reply to `update_check` when a newer release exists. The speaker offers it on screen (Install / Dismiss) |
| `ping` | | Reply with `{"type":"pong"}`. Sent every 25 s |

**Device → server**

| type | fields | meaning |
|---|---|---|
| `status` | `status: {state, firmware, ip, ssid, rssi, uptime_s, free_heap, free_psram, battery, charging, volume, muted, brightness, config_version, message}` | Send on every state change and at least every 30 s. Every field is optional |
| `log` | `lines: [{ts, level, tag, message}]` | Batches of up to 200 lines. `level` is one of E/W/I/D/V. `ts` is Unix seconds, or 0 if the clock isn't set |
| `event` | `name`, `data` | Notable events such as `wake`, `button`, `touch`, `wifi_changed` |
| `settings` | `volume` | A setting changed on the speaker itself (touch screen). The server stores it in the config without bumping the version or pushing it back |
| `update_check` | | Sent once after boot. The server checks the firmware repository and may answer `update_available` |
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
    "wifi": [{"ssid": "Thing Tank", "password": "…"}],
    "timezone": "MST7MDT,M3.2.0,M11.1.0",
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
      "listening": {…}, "thinking": {…}, "speaking": {…}, "sleeping": {…}, "error": {…}}}
}
```

What the firmware has to do with it:

* **Theme.** Use the colours as given. They already include any overrides.
* **Layout.** Widgets sit on the 240×240 round panel, (0,0) is top-left, and they are drawn in list order.
  * `ring`: an arc around the edge whose colour shows the state. idle uses `surface`, listening `primary`, thinking `muted`, speaking `accent`, error `#ff5d5d`, sleeping is hidden.
  * `character`: the animation for the current state.
  * `clock`, `date`: local time, using `timezone` and `clock_24h`.
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
  its saved list. It must never delete the network it is connected to unless it has just connected successfully to
  one from the list. A long press on BOOT always opens the local provisioning portal as a way back in.
* After applying a config, the device reports `config_version` in its next `status`.

## 5. Voice

A tap (later the wake word) starts an utterance. Audio is 16 kHz, 16-bit, mono, little-endian PCM in both directions,
sent as binary WebSocket frames.

```
speaker                                     Harkenn
voice_start {session, rate, local_time}  ->
binary frames (mic, ~100 ms each)        ->
voice_end {session}                      ->           speech-to-text (Whisper), reply, text-to-speech (Piper)
                                         <-  voice_result {session, heard, reply}
                                         <-  speak_start {session, rate, bytes, text}
                                         <-  binary frames (speech)
                                         <-  speak_end {session}
```

* The speaker ends the utterance itself: on a tap, after `audio.silence_s` without speech, or after 30 s. If it heard
  no speech at all it sends `voice_cancel` instead of `voice_end`.
* `local_time` (ISO 8601, no zone) is the speaker's clock, so answers use its time zone.
* `voice_result` with an empty `heard` means nothing was understood: the speaker plays its cancel sound.
* `voice_error {session, message}` means the pipeline failed.
* Speech is sent at most about 3 s ahead of playback. `speak_start` can also arrive without an utterance (the admin
  UI's *Say it*, announcements).
* A tap while listening ends the utterance; while waiting, it sends `voice_cancel`; while speaking, it stops playback
  and sends `speak_stop`.
