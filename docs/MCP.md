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

Hermes names the tools `mcp_harkenn_list_speakers`, `mcp_harkenn_speak` and `mcp_harkenn_ring`. For guidance on
when to use which, copy [skills/harkenn-speakers](skills/harkenn-speakers/SKILL.md) into Hermes' skills folder
(`~/.hermes/skills/`).

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
  hears "Thanks, I'll pass that on." An empty `reply` means nothing was understood.

The call takes as long as the ringing plus the reply, so give your client a tool timeout of about 2 minutes.

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
