"""Time zones by name (America/Edmonton), for people, and the POSIX TZ strings the speakers use.

A speaker keeps its clock with a POSIX rule ("MST7MDT,M3.2.0,M11.1.0"). People pick a place, so the config keeps the
zone's name too (DeviceConfig.timezone_name), and Harkenn turns it into the rule from the system's time zone database
each time it sends the config: when the rules change (a province drops daylight saving time), an updated database
puts the speakers right without anyone touching them. The rule is the footer of the zone's TZif file (RFC 8536).
Without a database (some Windows setups), a short built-in list stands in.
"""
from __future__ import annotations

import functools
import zoneinfo
from pathlib import Path

# From tzdata 2026 (read from the Harkenn image). Only used where the system has no time zone database.
FALLBACK = {
    "America/St_Johns": "NST3:30NDT,M3.2.0,M11.1.0", "America/Halifax": "AST4ADT,M3.2.0,M11.1.0",
    "America/Toronto": "EST5EDT,M3.2.0,M11.1.0", "America/New_York": "EST5EDT,M3.2.0,M11.1.0",
    "America/Chicago": "CST6CDT,M3.2.0,M11.1.0", "America/Winnipeg": "EST5", "America/Regina": "CST6",
    "America/Edmonton": "CST6", "America/Denver": "MST7MDT,M3.2.0,M11.1.0", "America/Phoenix": "MST7",
    "America/Vancouver": "MST7", "America/Los_Angeles": "PST8PDT,M3.2.0,M11.1.0",
    "America/Anchorage": "AKST9AKDT,M3.2.0,M11.1.0", "Pacific/Honolulu": "HST10", "America/Mexico_City": "CST6",
    "America/Bogota": "<-05>5", "America/Sao_Paulo": "<-03>3", "America/Argentina/Buenos_Aires": "<-03>3",
    "Europe/London": "GMT0BST,M3.5.0/1,M10.5.0", "Europe/Dublin": "IST-1GMT0,M10.5.0,M3.5.0/1",
    "Europe/Lisbon": "WET0WEST,M3.5.0/1,M10.5.0", "Europe/Paris": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Europe/Berlin": "CET-1CEST,M3.5.0,M10.5.0/3", "Europe/Madrid": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Europe/Rome": "CET-1CEST,M3.5.0,M10.5.0/3", "Europe/Amsterdam": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Europe/Stockholm": "CET-1CEST,M3.5.0,M10.5.0/3", "Europe/Warsaw": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Europe/Athens": "EET-2EEST,M3.5.0/3,M10.5.0/4", "Europe/Helsinki": "EET-2EEST,M3.5.0/3,M10.5.0/4",
    "Europe/Kyiv": "EET-2EEST,M3.5.0/3,M10.5.0/4", "Europe/Istanbul": "<+03>-3", "Europe/Moscow": "MSK-3",
    "Africa/Lagos": "WAT-1", "Africa/Cairo": "EET-2EEST,M4.5.5/0,M10.5.4/24", "Africa/Johannesburg": "SAST-2",
    "Africa/Nairobi": "EAT-3", "Asia/Dubai": "<+04>-4", "Asia/Karachi": "PKT-5", "Asia/Kolkata": "IST-5:30",
    "Asia/Dhaka": "<+06>-6", "Asia/Bangkok": "<+07>-7", "Asia/Jakarta": "WIB-7", "Asia/Singapore": "<+08>-8",
    "Asia/Shanghai": "CST-8", "Asia/Hong_Kong": "HKT-8", "Asia/Manila": "PST-8", "Asia/Tokyo": "JST-9",
    "Asia/Seoul": "KST-9", "Australia/Perth": "AWST-8", "Australia/Darwin": "ACST-9:30",
    "Australia/Adelaide": "ACST-9:30ACDT,M10.1.0,M4.1.0/3", "Australia/Brisbane": "AEST-10",
    "Australia/Sydney": "AEST-10AEDT,M10.1.0,M4.1.0/3", "Australia/Melbourne": "AEST-10AEDT,M10.1.0,M4.1.0/3",
    "Pacific/Auckland": "NZST-12NZDT,M9.5.0,M4.1.0/3", "UTC": "UTC0",
}
REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian",
           "Pacific")


def _file(name: str) -> Path | None:
    for root in zoneinfo.TZPATH:
        path = Path(root) / name
        if path.is_file():
            return path
    return None


@functools.lru_cache(maxsize=1024)
def posix(name: str) -> str:
    """The POSIX TZ rule for a zone name, or "" if it isn't known."""
    if not name or ".." in name or name.startswith("/"):
        return ""
    path = _file(name)
    if path is None:
        return FALLBACK.get(name, "")
    try:
        data = path.read_bytes()
    except OSError:
        return FALLBACK.get(name, "")
    if not data.startswith(b"TZif") or data[4:5] < b"2":
        return FALLBACK.get(name, "")
    footer = data.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode("ascii", "replace")
    return footer if footer and footer != data.decode("ascii", "replace") else FALLBACK.get(name, "")


@functools.lru_cache(maxsize=1)
def names() -> list[str]:
    """Zone names people would pick (Region/City, and UTC), sorted."""
    try:
        found = zoneinfo.available_timezones() if any(Path(p).is_dir() for p in zoneinfo.TZPATH) else set()
    except Exception:  # pragma: no cover
        found = set()
    zones = {z for z in found if z.split("/", 1)[0] in REGIONS and "/" in z} or set(FALLBACK)
    return sorted(zones | {"UTC"}, key=lambda z: (z != "UTC", z))

