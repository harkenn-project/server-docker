"""What the household modules read from elsewhere: calendar subscriptions (ICS links from Google, iCloud, Outlook...)
and news feeds (RSS or Atom). Both are fetched at most every REFRESH_S and kept in memory."""
from __future__ import annotations

import html
import logging
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

log = logging.getLogger("harkenn.household")

REFRESH_S = 30 * 60
TIMEOUT_S = 15
MAX_BYTES = 5_000_000
USER_AGENT = "Harkenn/1 (smart speaker; calendar and news)"


def fetch(url: str) -> bytes:
    url = re.sub(r"^webcal://", "https://", url.strip(), flags=re.I)
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("links must start with http(s):// or webcal://")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("that's too large")
    return data


class Cache:
    """url -> (fetched at, parsed), refreshed when older than REFRESH_S. A failed fetch keeps what was there."""

    def __init__(self, parse) -> None:
        self.parse = parse
        self._data: dict[str, tuple[float, Any]] = {}
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, url: str, fresh: bool = False) -> Any:
        with self._lock:
            hit = self._data.get(url)
        if hit and not fresh and time.time() - hit[0] < REFRESH_S:
            return hit[1]
        try:
            parsed = self.parse(fetch(url))
        except Exception as e:
            log.warning("household: couldn't read %s: %s", url, e)
            self.errors[url] = str(e)[:200]
            return hit[1] if hit else None
        self.errors.pop(url, None)
        with self._lock:
            self._data[url] = (time.time(), parsed)
        return parsed


# calendars

def parse_ics(data: bytes) -> Any:
    import icalendar
    return icalendar.Calendar.from_ical(data)


def _local(value: Any, tz: tzinfo) -> tuple[datetime, bool]:
    """A start or end as a naive local datetime, and whether it's a whole day."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(tz)
        return value.replace(tzinfo=None), False
    return datetime(value.year, value.month, value.day), True


def ics_events(cal: Any, start: datetime, end: datetime, tz: tzinfo, source: str) -> list[dict[str, Any]]:
    """The events of a parsed calendar between two naive local datetimes, repeats expanded."""
    import recurring_ical_events
    out = []
    try:
        found = recurring_ical_events.of(cal).between(start.replace(tzinfo=tz), end.replace(tzinfo=tz))
    except Exception as e:
        log.warning("household: calendar %s: %s", source, e)
        return []
    for ev in found:
        if not ev.get("DTSTART"):
            continue
        s, all_day = _local(ev["DTSTART"].dt, tz)
        e = _local(ev["DTEND"].dt, tz)[0] if ev.get("DTEND") else s + (timedelta(days=1) if all_day else timedelta())
        out.append({"title": str(ev.get("SUMMARY") or "(no title)")[:120], "start": s, "end": e, "all_day": all_day,
                    "where": str(ev.get("LOCATION") or "")[:120], "source": source})
    return out


def repeats(start: datetime, repeat: str, frm: datetime, to: datetime) -> list[datetime]:
    """The times a local event happens between frm and to (repeat: "", daily, weekly, monthly, yearly)."""
    if not repeat:
        return [start] if frm <= start < to else []
    from dateutil.rrule import DAILY, MONTHLY, WEEKLY, YEARLY, rrule
    freq = {"daily": DAILY, "weekly": WEEKLY, "monthly": MONTHLY, "yearly": YEARLY}.get(repeat)
    if freq is None:
        return [start] if frm <= start < to else []
    return list(rrule(freq, dtstart=start).between(frm - timedelta(seconds=1), to))


# news

def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    raw = "".join(el.itertext()) if len(el) else (el.text or "")
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def parse_feed(data: bytes) -> list[dict[str, str]]:
    """Headlines from RSS 2.0 or Atom: title and a short summary, newest first as the feed has them."""
    root = ET.fromstring(data)
    out = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = summary = ""
        for child in item:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "title":
                title = _text(child)
            elif name in ("description", "summary", "content") and not summary:
                summary = _text(child)
        if title:
            out.append({"title": title[:200], "summary": summary[:300]})
    return out[:30]


def day_label(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if d == today - timedelta(days=1):
        return "yesterday"
    if today < d < today + timedelta(days=7):
        return d.strftime("%A")
    return f"{d:%a %b} {d.day}"
