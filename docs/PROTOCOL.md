# Kotodama device protocol (v1)

This is the contract between the speaker firmware and the Kotodama server.
The firmware has exactly one thing hardcoded: the server URL, which is stored in the onboard settings
(default `https://kotodama.mylife.ninja`). Everything else comes from the server.

All paths are relative to that URL. JSON everywhere unless noted.

## 1. Boot: wait for the server

```
GET /api/v1/ping
200 {"ok": true, "server": "kotodama", "version": "0.1.0", "time": 1793430000.0}
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

The device shows `code` on its screen. An admin opens the Kotodama admin page, finds the waiting speaker and types the
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
| `command` | `command`, `args` | `reboot`, `identify` (flash the screen/LED), `refresh_config`, `set_volume` {volume}, `play_state` {state, seconds} |
| `ping` | | Reply with `{"type":"pong"}`. Sent every 25 s |

**Device → server**

| type | fields | meaning |
|---|---|---|
| `status` | `status: {state, firmware, ip, ssid, rssi, uptime_s, free_heap, free_psram, battery, charging, volume, brightness, config_version, message}` | Send on every state change and at least every 30 s. Every field is optional |
| `log` | `lines: [{ts, level, tag, message}]` | Batches of up to 200 lines. `level` is one of E/W/I/D/V. `ts` is Unix seconds, or 0 if the clock isn't set |
| `event` | `name`, `data` | Notable events such as `wake`, `button`, `touch`, `wifi_changed` |
| `config_request` | | Asks the server to send the current config again |
| `pong` | | Reply to `ping` |

### HTTP fallbacks

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/device/config` | The same payload as the WS `config` message. Returns 304 if `If-None-Match` equals the current version |
| POST | `/api/v1/device/status` | Body is the `status` object |
| POST | `/api/v1/device/logs` | Body is `{"lines": [...]}` |
| POST | `/api/v1/device/recordings?kind=utterance` | Raw body. `Content-Type` is `audio/wav`, `audio/ogg`, `audio/opus`, `audio/L16` or `application/octet-stream`. Returns 201 `{"id"}` |

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
               "urls": {"144": "https://kotodama.mylife.ninja/assets/characters/default/idle.gif?size=144&v=3"}},
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
  `source`. The URLs are versioned, so the device can cache by URL. The files are GIFs that are already the size of
  the widget, with 1-bit transparency, so no scaling happens on the device. Assets need no token.
* **Wi-Fi.** `wifi` is the full list of networks the server wants the device to know. The device adds new ones to
  its saved list. It must never delete the network it is connected to unless it has just connected successfully to
  one from the list. A long press on BOOT always opens the local provisioning portal as a way back in.
* After applying a config, the device reports `config_version` in its next `status`.
