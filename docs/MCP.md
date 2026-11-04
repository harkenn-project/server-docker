# Harkenn's MCP server

Harkenn has a built-in [MCP](https://modelcontextprotocol.io) server, so an AI agent (Hermes Agent, Claude, or any
other MCP client) can use the speakers in your home: announce something, or ring the speakers like a phone and talk
to whoever answers.

This is the opposite direction from the assistant setting on the **Voice** page. That setting decides who answers
when someone talks *to a speaker*. The MCP server lets an agent start the conversation itself.

## Connecting

- **Address:** `https://<your Harkenn server>/mcp` (Streamable HTTP). Agents on the same network or cluster can use
  the server's internal address, e.g. `http://harkenn:8000/mcp`.
- **Authentication:** `Authorization: Bearer <token>`. Make a token on the admin **Agents** page; it's shown once.
  A token can also come from the `HARKENN_MCP_TOKEN` environment variable (e.g. a Kubernetes secret shared with the
  agent).

The server is stateless and answers with plain JSON, so any MCP client that speaks Streamable HTTP works.

### Hermes Agent

In `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  harkenn:
    url: "http://harkenn:8000/mcp"
    headers:
      Authorization: "Bearer hk_..."
    timeout: 120   # a ring waits up to 30 s for an answer, then up to ~45 s for a spoken reply
```

That's all an agent needs: Harkenn describes itself through the connection. Its instructions (sent when a client
connects) say what the speakers can do, and every tool's description says how to use it, so any MCP-capable assistant
finds "play the rain soundscape" or "set a timer in the kitchen" without anything else installed. Hermes names the
tools `mcp__harkenn__speak` and so on.

A [skill](skills/harkenn-speakers/SKILL.md) adds manners (when to ring rather than speak, writing for a voice, what
to say when something isn't on a card). It's optional, and deliberately doesn't list the tools, so it can't go out of
date: if you use it, fetch it fresh rather than keeping a copy.

### Claude Code

```sh
claude mcp add --transport http harkenn https://harkenn.example.com/mcp --header "Authorization: Bearer hk_..."
```

## Tools

### `list_speakers()`

Every speaker, with its `id`, `name` (usually the room), whether it's `online`, what it's doing (`state`: idle,
listening, thinking, speaking, sleeping) and whether its microphone is muted (`mic_muted`). An online speaker can
still have no one near it. `agents` says what agents may do there now: `ring_and_speak`, `speak_only` or
`do_not_disturb`.

### `speak(text, speaker="all")`

Says `text` aloud on one speaker (its name or id) or on all of them. It returns at once, and the speech plays over
the next few seconds. Speakers that are in the middle of a conversation are skipped, not interrupted.

**No one confirms hearing it.** The result always tells the agent to presume that no one heard it. If it matters that
someone gets the message, the agent should use `ring`.

```json
{"spoken_on": ["Kitchen"], "skipped": ["Office (offline)"], "note": "Presume no one heard this: ..."}
```

### `ring(speaker="all", seconds=30, message="", wait_for_reply=false)`

Rings one speaker or all of them for 5–30 seconds, like a phone. The speaker plays a ring every 3 seconds and shows
"Tap to answer". The first tap answers the ring, and the other speakers stop ringing.

- **No answer:** `{"answered": false, "note": "... Presume no one is there and no one heard."}`
- **Answered:** `{"answered": true, "answered_on": "Kitchen"}`. If `message` is given, it's spoken on that speaker.
- **With `wait_for_reply`:** after the message (or "Go ahead." without one), the speaker listens. What the person
  says comes back as `reply`, transcribed by speech-to-text. That speaker's assistant doesn't answer it; the person
  hears "Thanks, I'll pass that on." An empty `reply` means nothing was understood. If the speaker shows a screen
  with buttons (docs/SCREEN.md), a tap counts as the reply: `(tapped "Yes" on the screen)`.

The call takes as long as the ringing plus the reply, so give your client a tool timeout of about 2 minutes.

### `list_timers(speaker="all")`, `set_timer(duration, label, speaker)`, `set_alarm(time_of_day, label, days, date, speaker)`, `cancel_timer(which, speaker)`

Timers and alarms live on each speaker and ring there, even if Harkenn is down. `duration` is like `"9m"`, `"1h30m"`
or seconds; `time_of_day` `"07:30"` or `"7:30 am"`; `days` `"daily"`, `"weekdays"`, `"weekends"` or names
(`"mon,wed"`), empty for once; `date` `"YYYY-MM-DD"` for one day. `which` is a label, an id, `"all"`, `"timers"` or
`"alarms"`. `list_timers` also gives what happened to them in the last half hour (rang, stopped, missed) and unread
notifications. With more than one speaker, name one.

### `notify(text, title="", speaker="all", seconds=12)`

A notification card on the speaker's screen: it drops in with a soft chime, and an envelope stays at the top of the
screen until someone taps it. Nothing is spoken (use `speak` for that). The assistant on that speaker is told about
unread notifications.

### `get_weather()`

The weather where the speakers are (the place set on the admin **Server** page, from Open-Meteo, refreshed every 15
minutes): condition, temperature, feels-like, high and low, wind and gusts, humidity, precipitation, UV, air quality,
and `daily` for the next four days, in the units chosen there. `{"weather": {}}` when no place is set.

### `show_weather(speaker="all", condition, location, temp, ..., view, forecast, seconds=60)`

Shows the weather screen on one speaker or all of them: an animated picture of the weather with the numbers, drawn by
Harkenn (the speaker only plays it). Every field is optional. With none it shows the weather here now;
`view="forecast"` shows the next four days. Fields given win over the live weather, and a different `location`
takes nothing from it, so an agent can show any weather it has. The conditions and fields are in
[SCREEN.md](SCREEN.md#the-weather-screen). Skips speakers that are offline or set to do not disturb.

### `household_items(module, person="", include_done=false)`, `household_change(module, action, person="")`

The household modules ([HOUSEHOLD.md](HOUSEHOLD.md)), when they're turned on: `memories`, `todo`, `shopping`,
`reminders`, `calendar`, `messages` and `chores`. They're shared with the people at home and with Harkenn's own
assistant, so an agent adding to the shopping list adds to the same list. `person` is a name from the **People**
page: it adds that person's own memories, reminders and events, and is who `"for": "me"` means. `action` is what
Harkenn's own assistant writes in its markers, e.g. `{"add": ["milk", "eggs"]}`, `{"done": "plumber"}`,
`{"text": "bins", "at": "2026-11-04 19:00", "repeat": "weekly"}`; the tool's description lists them all.

### `calendar_events(start, end="", person="")`, `news_headlines(topic="")`, `morning_briefing(speaker="", person="")`

What's on between two dates (Harkenn's events and its subscribed calendars, repeats expanded), headlines from the
news feeds set up in Harkenn, and everything for a morning briefing at once, as it's set up for that speaker's group.

### `media_library(speaker="all")`, `play_media(what, kind="", speaker="", minutes=0, shuffle=false)`, `control_media(action, speaker="", minutes=0, repeat="")`

Music albums, soundscapes and meditations on a speaker's micro SD card, if it has one: what's there and what's
playing, play one (`minutes` is a sleep timer), and pause, resume, stop, next, previous, repeat or a sleep timer.
People can ask the speakers directly too ("play rain for an hour", "pause", "next"): those requests are handled by
Harkenn before they reach any assistant.

## Do not disturb

Each speaker's page has an **Agents** section, set by the people who live there, and enforced by the server:

- **Agents may:** ring it and speak on it (the default), speak on it but not ring it, or nothing at all (do not
  disturb).
- **Quiet hours:** a daily window, in the server's time zone, when the speaker is do not disturb. It can span
  midnight, e.g. 22:00 to 07:00.

`speak` and `ring` skip speakers that don't allow them, and tell the agent so, asking it not to try to get around
it. On top of that, agents can ring any one speaker at most 6 times an hour.

## Prompts and resources

- **Prompt `announce(message)`:** announce something on every speaker.
- **Prompt `ask_someone_at_home(question)`:** ring every speaker, ask whoever answers, and report what they said.
- **Resource `harkenn://guide`:** the same guidance as the server's instructions.

## Speaking well

The words are spoken by a text-to-speech voice, so agents should write short, plain sentences with no lists,
markdown, emoji or URLs. Ringing is loud and interrupts people: don't ring at night, or over and over, unless it's
urgent.

## Security

A token lets its holder make every speaker in the home talk or ring. Give each agent its own token, so you can revoke
one without the others. Tokens are stored hashed, and the **Agents** page shows when each was last used. Only expose
`/mcp` outside your network if you need to.
