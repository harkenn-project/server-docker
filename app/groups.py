"""Speaker groups: shared settings, with each speaker's own overrides.

Every speaker is in one group. A group holds a whole DeviceConfig; a speaker follows it except for the settings it
overrides, listed in the device's `overrides` as dotted paths ("audio.volume", "display.colors"). Wi-Fi networks
combine instead: a speaker gets its group's networks plus its own, and an own network with the same name as one of
the group's replaces it on that speaker (say, with a static IP). Own networks are listed as "wifi:<ssid>".

The device row keeps the result (`config`, what the speaker gets), so everything that reads a speaker's settings
reads them as before. The speaker's own values are the ones at its override paths in that config. When a group
changes, each member's config is put together again from the group and its overrides.

Anything that changes one speaker's settings (the admin, the speaker's own page, Home Assistant, the volume from its
touch screen) makes those settings that speaker's own, so a later change to the group doesn't undo it.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel

from . import timezones
from .models import ColorOverrides, DeviceConfig

GROUPS_KEY = "groups"  # settings table: [{"id", "name", "config"}]
DEFAULT_KEY = "default_group"  # settings table: the group new speakers join
FIRST_GROUP = {"id": "home", "name": "Home"}
MAX_GROUPS = 32


def _leaves(model: type[BaseModel], prefix: str = "") -> list[str]:
    out = []
    for name, f in model.model_fields.items():
        kind = f.annotation
        if not prefix and name == "wifi":
            continue
        if isinstance(kind, type) and issubclass(kind, BaseModel) and kind is not ColorOverrides:
            out += _leaves(kind, f"{prefix}{name}.")
        else:
            out.append(prefix + name)
    return out


LEAVES = tuple(_leaves(DeviceConfig))  # the settings a speaker can override, one by one


def get_path(obj: dict[str, Any], path: str) -> Any:
    for key in path.split("."):
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    *head, last = path.split(".")
    for key in head:
        obj = obj.setdefault(key, {})
    obj[last] = copy.deepcopy(value)


def valid_overrides(paths: Any) -> set[str]:
    return {p for p in paths or [] if isinstance(p, str) and (p in LEAVES or (p.startswith("wifi:") and len(p) > 5))}


def differences(config: dict[str, Any], group: dict[str, Any]) -> set[str]:
    """The overrides that make `group` into `config`."""
    out = {p for p in LEAVES if get_path(config, p) != get_path(group, p)}
    theirs = {n["ssid"]: n for n in group.get("wifi", [])}
    out |= {f"wifi:{n['ssid']}" for n in config.get("wifi", []) if theirs.get(n["ssid"]) != n}
    return out


def compose(group: dict[str, Any], own: dict[str, Any], overrides: set[str]) -> dict[str, Any]:
    """A speaker's config: its group's, with its own values at its override paths, and its own Wi-Fi first."""
    out = copy.deepcopy(group)
    for path in overrides:
        if path in LEAVES:
            set_path(out, path, get_path(own, path))
    mine = {p[5:] for p in overrides if p.startswith("wifi:")}
    own_wifi = [n for n in own.get("wifi", []) if n["ssid"] in mine]
    out["wifi"] = (own_wifi + [n for n in group.get("wifi", []) if n["ssid"] not in mine])[:8]
    return with_rule(DeviceConfig.model_validate(out).model_dump())


def with_rule(config: dict[str, Any]) -> dict[str, Any]:
    """A named time zone sets the clock rule (timezones.py)."""
    rule = timezones.posix(config.get("timezone_name") or "")
    if rule:
        config["timezone"] = rule
    return config


# storage

def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "group"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def ensure(db: Any) -> None:
    """The first time: one group from the old defaults, with every speaker in it and its differences kept as
    overrides, so nothing changes for any speaker."""
    if db.get_setting(GROUPS_KEY) is not None:
        return
    raw = db.get_setting("default_config")
    try:
        base = DeviceConfig.model_validate(raw).model_dump() if raw else DeviceConfig().model_dump()
    except Exception:
        base = DeviceConfig().model_dump()
    db.set_setting(GROUPS_KEY, [{**FIRST_GROUP, "config": base}])
    db.set_setting(DEFAULT_KEY, FIRST_GROUP["id"])
    for device in db.list_devices():
        config = DeviceConfig.model_validate(device["config"]).model_dump()
        db.set_device_group(device["id"], FIRST_GROUP["id"], sorted(differences(config, base)))


def all_groups(db: Any) -> list[dict[str, Any]]:
    groups = db.get_setting(GROUPS_KEY) or []
    for g in groups:
        g["config"] = DeviceConfig.model_validate(g["config"]).model_dump()  # with settings added since
    return groups


def default_id(db: Any) -> str:
    groups = db.get_setting(GROUPS_KEY) or []
    wanted = db.get_setting(DEFAULT_KEY)
    ids = [g["id"] for g in groups]
    return wanted if wanted in ids else (ids[0] if ids else FIRST_GROUP["id"])


def get(db: Any, group_id: str | None) -> dict[str, Any] | None:
    return next((g for g in all_groups(db) if g["id"] == group_id), None)


def of_device(db: Any, device: dict[str, Any]) -> dict[str, Any]:
    """The group a speaker follows (the default one if its group is gone)."""
    return get(db, device.get("group_id")) or get(db, default_id(db)) or {**FIRST_GROUP,
                                                                         "config": DeviceConfig().model_dump()}


def create(db: Any, name: str, copy_from: str | None = None) -> dict[str, Any]:
    groups = all_groups(db)
    if len(groups) >= MAX_GROUPS:
        raise ValueError(f"at most {MAX_GROUPS} groups")
    source = next((g for g in groups if g["id"] == copy_from), None) or next(
        (g for g in groups if g["id"] == default_id(db)), None)
    group = {"id": _slug(name, {g["id"] for g in groups}), "name": name.strip(),
             "config": copy.deepcopy(source["config"]) if source else DeviceConfig().model_dump()}
    db.set_setting(GROUPS_KEY, groups + [group])
    return group


def save(db: Any, group_id: str, *, name: str | None = None, config: dict[str, Any] | None = None) -> list[str]:
    """Change a group, and its members' configs with it. Returns the members whose config changed."""
    groups = all_groups(db)
    group = next((g for g in groups if g["id"] == group_id), None)
    if group is None:
        raise KeyError(group_id)
    if name is not None:
        group["name"] = name.strip()
    if config is not None:
        group["config"] = with_rule(DeviceConfig.model_validate(config).model_dump())
    db.set_setting(GROUPS_KEY, groups)
    return _recompose(db, group_id)


def delete(db: Any, group_id: str) -> list[str]:
    """Delete a group; its speakers move to the default group. Returns the speakers whose config changed."""
    if group_id == default_id(db):
        raise ValueError("new speakers join this group: choose another group for them first")
    groups = all_groups(db)
    if not any(g["id"] == group_id for g in groups):
        raise KeyError(group_id)
    db.set_setting(GROUPS_KEY, [g for g in groups if g["id"] != group_id])
    moved = [d["id"] for d in db.list_devices() if d.get("group_id") == group_id]
    target = default_id(db)
    for device_id in moved:
        db.set_device_group(device_id, target, None)
    return _recompose(db, target, only=set(moved))


def set_default(db: Any, group_id: str) -> None:
    if get(db, group_id) is None:
        raise KeyError(group_id)
    db.set_setting(DEFAULT_KEY, group_id)


def _recompose(db: Any, group_id: str, only: set[str] | None = None) -> list[str]:
    changed = []
    group = get(db, group_id)
    for device in db.list_devices():
        if (device.get("group_id") or default_id(db)) != group_id or (only is not None and device["id"] not in only):
            continue
        config = compose(group["config"], device["config"], set(device.get("overrides") or []))
        if config != device["config"]:
            db.update_device(device["id"], name=device["name"], config=config)
            changed.append(device["id"])
    return changed


def save_device(db: Any, device_id: str, config: dict[str, Any], *, name: str | None = None,
                group_id: str | None = None, overrides: list[str] | None = None) -> int | None:
    """Store a speaker's settings. With `overrides` (the admin's speaker page) those are its own settings, and
    config["wifi"] is its own networks. Without (its own page, Home Assistant), whatever differs from its group becomes
    its own, on top of what already was. Returns the new config version."""
    device = db.get_device(device_id)
    if device is None:
        return None
    group = get(db, group_id) if group_id else of_device(db, device)
    if group is None:
        raise KeyError(group_id)
    config = DeviceConfig.model_validate(config).model_dump()
    if overrides is None:
        before = set(device.get("overrides") or [])
        present = {f"wifi:{n['ssid']}" for n in config["wifi"]}
        mine = {p for p in before if not p.startswith("wifi:") or p in present} | differences(config, group["config"])
    else:
        mine = valid_overrides(overrides) - {p for p in overrides if p.startswith("wifi:")}
        mine |= {f"wifi:{n['ssid']}" for n in config["wifi"]}
    effective = compose(group["config"], config, mine)
    db.set_device_group(device_id, group["id"], sorted(mine))
    return db.update_device(device_id, name=(name if name is not None else device["name"]), config=effective)
