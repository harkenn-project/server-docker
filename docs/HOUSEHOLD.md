# Household modules

A chat model on its own forgets everything between conversations and knows nothing about your home. These modules
give it what a household assistant needs, kept by Harkenn, on the **Modules** page of the admin UI:

| Module | What it does |
|---|---|
| Memories (on by default) | "Remember that my sister is Anna." Kept for the person talking, or the household. |
| To-do | The household's to-do list, with due dates. |
| Shopping list | "Add milk and two lemons." Tick things off at the shop from its admin page. |
| Reminders | Said and shown on a speaker at a time, or rung like an alarm (with Stop); once, daily, on weekdays, weekly or monthly. |
| Calendar | Harkenn's own events (with yearly repeats for birthdays), plus your existing calendars, read-only. |
| Messages | "Tell Jules dinner's ready": passed on when the voiceprints recognize Jules, or shown on a speaker now. |
| Chores | A rota: whose turn it is, moved on when someone says they did it, with an optional daily reminder. |
| Morning briefing | "Good morning" (or stopping an alarm between 4 and 11): the weather, today's events, reminders, to-dos, chores, messages and news. What it covers can differ per group. |
| News headlines | Headlines from the RSS or Atom feeds you add. |

Everything the modules keep has its own admin page, where it can be added to, changed and deleted.

## Who things belong to

Memories, reminders and events belong to someone when the voiceprints on the **People** page recognize who's
talking ("remind me", "my dentist appointment"), and to the household otherwise. The assistant is told the
household's things and the speaker's own, never someone else's. Lists and chores are the household's.

Without voiceprints everything is the household's, and messages are told to whoever asks, with the instruction to
pass each on only to the person it's for.

## How the assistant uses them

Like timers ([SCREEN.md](SCREEN.md) and `app/timers.py`), with markers in its reply that are never spoken:

```
[[memory {"remember": "Marika's sister is Anna", "for": "me"}]]
[[shopping {"add": ["milk", "2 lemons"]}]]
[[reminder {"text": "take the bins out", "at": "2026-11-04 19:00", "repeat": "weekly"}]]
[[calendar {"add": "Dentist", "start": "2026-11-06 14:30", "end": "15:30", "for": "me"}]]
[[message {"to": "Jules", "text": "dinner is ready"}]]
[[chore {"done": "dishes", "by": "Jules"}]]
```

The instructions describe only the modules that are on, and stay the same from one request to the next, so providers
cache them. What the assistant needs to know (what's remembered, the lists, reminders, today's and tomorrow's events,
messages waiting, whose turn the chores are) goes on the newest message, and only when it has changed since the
conversation was last told.

Some answers need looking up first: the news, the briefing, and other days of the calendar. For those the assistant
replies with only a marker (`[[news]]`, `[[briefing]]`, `[[calendar {"find": "2026-11-10", "to": "2026-11-16"}]]`);
Harkenn finds what it asked for and asks again, and the second reply is what's said. That takes one more request.

## Agents

An agent such as Hermes has its own memory, tools and skills, so its instructions don't include the modules. It can
use them through Harkenn's MCP server instead ([MCP.md](MCP.md)): `household_items`, `household_change`,
`calendar_events`, `news_headlines` and `morning_briefing`, on the same lists and calendar as everyone else.

## Calendars and news

On the **Modules** page:

- **Calendar subscriptions** take a calendar's ICS address: in Google Calendar, Settings, the calendar, *Secret
  address in iCal format*; in iCloud, share the calendar publicly; in Outlook, *Publish a calendar* (ICS). They're
  read-only, read every 30 minutes, with repeating events expanded.
- **News feeds** take RSS or Atom addresses. *Show the headlines* reads them now.

Times are local to the time zone of the group new speakers join.
