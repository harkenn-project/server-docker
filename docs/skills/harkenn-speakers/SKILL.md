---
name: harkenn-speakers
description: The Harkenn smart speakers in this home - talk to people through them, ring them, timers and alarms, notifications and the weather on their screens, the household's lists, reminders and calendar, and playing music, soundscapes and meditations from a speaker's SD card. Needs Harkenn's MCP server (tools mcp__harkenn__*).
---

# Harkenn speakers

Harkenn runs the smart speakers in this home. Each speaker listens, talks, has a small round screen, and can play
music, soundscapes and meditations from a micro SD card if it has one.

**What you can do is whatever the `harkenn` MCP server's tools offer: their names and descriptions are the full,
current list.** This skill doesn't list them (a list here would go out of date); it only adds how to use them well.

## Playing music and sounds

The speakers play from their own SD cards: albums (e.g. "Background", soft piano), soundscapes that loop (rain, a
thunderstorm, a stream, a campfire, white noise...) and meditations. That's separate from any other music service in
the home: a speaker doesn't need it.

- When someone asks for music or sounds on a speaker, play it with the media tools: they take loose names ("rain",
  "birds", "piano") and say what's on the card when nothing fits.
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
