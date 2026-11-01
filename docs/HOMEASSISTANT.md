# Home Assistant

Harkenn can show every speaker in Home Assistant as a device, through MQTT discovery. The speakers themselves
don't change: the Harkenn server talks to Home Assistant's MQTT broker for them.

## Setting it up

1. Home Assistant needs an MQTT broker and its **MQTT** integration (Settings → Devices & services). The Mosquitto
   add-on is the usual choice.
2. On Harkenn's **Server** page, under *Home Assistant*, tick *Connect to Home Assistant* and enter the broker's
   address: `mqtt://user:password@homeassistant.local:1883` (`mqtts://` for TLS). Or set `HARKENN_MQTT_URL` in
   the server's environment, which applies until the Server page saves its own settings.
3. The status should read *connected*, and each speaker appears in Home Assistant under its own name.

## What each speaker has

| Entity | | |
|---|---|---|
| Activity | sensor | `idle`, `listening`, `thinking`, `speaking` or `ringing`, as it happens |
| Online | binary sensor | connected to Harkenn |
| Mic muted | binary sensor | |
| Volume | number | 0–100; changing it changes the speaker |
| Agents | select | what AI agents may do on it: *Ring and speak*, *Speak only*, *Do not disturb* |
| Say | notify | speaks a message (`notify.send_message`) |
| Identify | button | blinks the screen |
| Restart | button | |
| Wi-Fi signal | sensor | dBm (diagnostic) |
| Battery | sensor | on speakers that have one |

Activity comes from the server as it happens, so automations can react straight away, for example to turn the
TV down while someone talks to a speaker:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.kitchen_activity
    to: listening
actions:
  - action: media_player.volume_mute
    target: { entity_id: media_player.tv }
    data: { is_volume_muted: true }
```

And announcements:

```yaml
actions:
  - action: notify.send_message
    target: { entity_id: notify.kitchen_say }
    data: { message: "The washing machine is done." }
```

## Topics

| Topic | |
|---|---|
| `homeassistant/<component>/harkenn_<id>/<object>/config` | discovery (retained); the prefix is set on the Server page |
| `harkenn/status` | `online` / `offline` (the last will) |
| `harkenn/<id>/state` | JSON with everything above (retained) |
| `harkenn/<id>/<object>/set` | commands: `volume`, `agents`, `say`, `identify`, `restart` |

When Home Assistant restarts (`homeassistant/status` `online`), Harkenn sends discovery again. Forgetting a
speaker in Harkenn removes it from Home Assistant.
