---
name: harkenn-speakers
description: The Harkenn smart speakers in this home - talk to people through them, ring them, timers and alarms, notifications and the weather on their screens, the household's lists, reminders and calendar, and playing music, soundscapes and meditations from a speaker's SD card. Needs Harkenn's MCP server (tools mcp__harkenn__*).
---

# Harkenn speakers

Harkenn runs the smart speakers in this home. Each speaker listens, talks, has a small round screen, and can play
music, soundscapes and meditations from a micro SD card if it has one. Its MCP server, `harkenn`, has these tools:

| Tool | Use it to |
|---|---|
| `list_speakers` | See the speakers, their rooms (the names), which are online, and what each is doing |
| `speak` | Say something aloud on a speaker, when no response is needed |
| `ring` | Reach a person: ring like a phone, then speak and optionally hear their reply |
| `notify` | Put a notification card on a speaker's screen (no speech); an envelope stays until someone looks |
| `list_timers`, `set_timer`, `set_alarm`, `cancel_timer` | Timers and alarms, which live on a speaker and ring there: "9m", "07:30" on "weekdays", cancel by label |
| `get_weather`, `show_weather` | The weather here now and for the coming days; an animated weather screen on a speaker |
| `media_library` | What each speaker can play: the music albums, soundscapes and meditations on its SD card, and what's playing now |
| `play_media` | Play one of those on a speaker, by name, with an optional sleep timer in minutes |
| `control_media` | Pause, resume, stop, next, previous, repeat, or a sleep timer, on a speaker that's playing |
| `household_items`, `household_change` | The household's memories, to-do and shopping lists, reminders, calendar, messages and chores (the ones turned on in Harkenn) |
| `calendar_events`, `news_headlines`, `morning_briefing` | The calendar for some days, the news from Harkenn's feeds, everything for a morning briefing |

## Playing music and sounds

The speakers play from their own SD cards: albums (e.g. "Background", soft piano), soundscapes that loop (rain, a
thunderstorm, a stream, a campfire, white noise...) and meditations. That's separate from any other music service in
the home: a speaker doesn't need it.

- When someone asks for music or sounds on a speaker, call `media_library` and play what fits with `play_media`.
  "Rain" means a soundscape with rain in its name; "something relaxing" can be the Background album or a soundscape.
- If it isn't on that speaker's card, say what is there, and that more can be sent to the card from the Media pages in
  Harkenn's admin. Don't say the speaker can't play anything.
- A speaker without an SD card can't play media: say so plainly.
- Simple requests said to a speaker ("play rain", "pause", "next") are handled by Harkenn before they reach you. If one
  does reach you, the words were probably misheard or it isn't on the card: check `media_library`.
- Music fades out by itself while a speaker listens or talks, so you don't need to pause it to speak.

## Choosing between speak and ring

- **Presume no one heard anything spoken with `speak`.** Harkenn can't tell whether anyone is in the room. Don't tell
  the user a message was delivered just because it was spoken. Say it was announced.
- If it matters that a person gets the message, or you need an answer, use `ring`. You'll learn whether someone
  answered, and on which speaker.
- If `ring` comes back unanswered, presume no one is home or no one could come to a speaker. Don't ring again straight
  away. Tell the user, or try another way to reach them.
- Some speakers are set to **do not disturb**, or to speak only, by the people who live here (`agents` in
  `list_speakers`). Respect it: never try to get around it, and don't ring other speakers to reach someone who asked
  not to be disturbed.

## Asking a question

Call `ring` with `message` set to the question and `wait_for_reply: true`:

```json
{"speaker": "all", "seconds": 30, "message": "Hi, it's Hermes. Do you want me to turn the porch light on?", "wait_for_reply": true}
```

- `reply` holds what the person said, transcribed by speech-to-text, so allow for small errors.
- An empty `reply` means they said nothing that was understood.
- Say who you are at the start of the message: the person hears it out of the blue.

## The household

The lists, reminders, calendar, messages and chores are shared with the people here and with Harkenn's own assistant.
Read them with `household_items` before answering about them, and add to them with `household_change` when asked.
Only the modules turned on in Harkenn exist.

## Writing for a speaker

- One to three short spoken sentences. No lists, markdown, emoji, URLs or symbols that don't read aloud.
- Spell out what a voice should say: "ten thirty", not "10:30 hrs".

## Being considerate

- Ringing is loud and interrupts people. Only ring when the user asked you to reach someone, or it's urgent. Avoid
  late night and early morning unless it's an emergency.
- Prefer ringing the speaker in the room you need (from `list_speakers`) over `all`, when you know where someone is.
- Never use the speakers to pretend to be someone else.
