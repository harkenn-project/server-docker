---
name: harkenn-speakers
description: Talk to the people at home through the Harkenn smart speakers. Use to announce something aloud, to reach someone who's at home, or to ask them a question and hear their answer. Needs Harkenn's MCP server (tools mcp__harkenn__list_speakers, mcp__harkenn__speak, mcp__harkenn__ring).
---

# Harkenn speakers

Harkenn runs the smart speakers in this home. Its MCP server, `harkenn`, has three tools.

| Tool | Use it to |
|---|---|
| `mcp__harkenn__list_speakers` | See the speakers, their rooms (the names), and which are online |
| `mcp__harkenn__speak` | Say something aloud when no response is needed |
| `mcp__harkenn__ring` | Reach a person: ring like a phone, then speak and optionally hear their reply |

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

## Writing for a speaker

- One to three short spoken sentences. No lists, markdown, emoji, URLs or symbols that don't read aloud.
- Spell out what a voice should say: "ten thirty", not "10:30 hrs".

## Being considerate

- Ringing is loud and interrupts people. Only ring when the user asked you to reach someone, or it's urgent. Avoid
  late night and early morning unless it's an emergency.
- Prefer ringing the speaker in the room you need (from `list_speakers`) over `all`, when you know where someone is.
- Never use the speakers to pretend to be someone else.
