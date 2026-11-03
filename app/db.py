"""SQLite storage for devices, adoption requests, logs, recordings and settings.

Every operation opens its own short-lived connection under one process-wide lock. The server runs as a single
replica with a handful of speakers, so simplicity wins over concurrency.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hardware TEXT NOT NULL DEFAULT '',
    firmware TEXT NOT NULL DEFAULT '',
    token_hash TEXT UNIQUE,
    adopted_at REAL NOT NULL,
    config TEXT NOT NULL,
    config_version INTEGER NOT NULL DEFAULT 1,
    last_seen REAL,
    last_ip TEXT,
    status TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS adoption_requests (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    code TEXT NOT NULL,
    hardware TEXT NOT NULL DEFAULT '',
    firmware TEXT NOT NULL DEFAULT '',
    ip TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    token TEXT
);
CREATE INDEX IF NOT EXISTS adoption_device ON adoption_requests(device_id, state);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    tag TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_device ON logs(device_id, id);
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS recordings_device ON recordings(device_id, created_at);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _device(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["config"] = json.loads(d["config"])
    d["status"] = json.loads(d["status"])
    return d


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    # settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # devices

    def list_devices(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM devices ORDER BY name COLLATE NOCASE").fetchall()
        return [_device(r) for r in rows]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            return _device(c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())

    def get_device_by_token(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as c:
            return _device(c.execute("SELECT * FROM devices WHERE token_hash=?", (token_hash,)).fetchone())

    def adopt_device(self, *, device_id: str, name: str, hardware: str, firmware: str,
                     token_hash: str, config: dict, now: float) -> None:
        """Create the device, or re-adopt an existing one (new token, config version bumped)."""
        with self._conn() as c:
            c.execute(
                """INSERT INTO devices(id, name, hardware, firmware, token_hash, adopted_at, config)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, hardware=excluded.hardware, firmware=excluded.firmware,
                     token_hash=excluded.token_hash, adopted_at=excluded.adopted_at,
                     config=excluded.config, config_version=devices.config_version + 1""",
                (device_id, name, hardware, firmware, token_hash, now, json.dumps(config)),
            )

    def update_device(self, device_id: str, *, name: str, config: dict) -> int | None:
        """Store a new name/config. Returns the new config version, or None if the device doesn't exist."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE devices SET name=?, config=?, config_version=config_version + 1 WHERE id=?",
                (name, json.dumps(config), device_id),
            )
            if cur.rowcount == 0:
                return None
            return c.execute("SELECT config_version FROM devices WHERE id=?", (device_id,)).fetchone()[0]

    def note_device_setting(self, device_id: str, section: str, key: str, value: Any) -> bool:
        """Record a setting the speaker changed itself (e.g. volume from the touch screen). The config version is not
        bumped, so nothing is pushed back to the speaker, which already has the value."""
        with self._conn() as c:
            row = c.execute("SELECT config FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                return False
            config = json.loads(row[0])
            if config.get(section, {}).get(key) == value:
                return False
            config.setdefault(section, {})[key] = value
            c.execute("UPDATE devices SET config=? WHERE id=?", (json.dumps(config), device_id))
            return True

    def touch_device(self, device_id: str, *, now: float, ip: str | None = None,
                     firmware: str | None = None, status: dict | None = None) -> None:
        sets, args = ["last_seen=?"], [now]
        if ip:
            sets.append("last_ip=?")
            args.append(ip)
        if firmware:
            sets.append("firmware=?")
            args.append(firmware)
        if status is not None:
            sets.append("status=?")
            args.append(json.dumps(status))
        with self._conn() as c:
            c.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=?", (*args, device_id))

    def delete_device(self, device_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM devices WHERE id=?", (device_id,))
            c.execute("DELETE FROM logs WHERE device_id=?", (device_id,))
            c.execute("DELETE FROM recordings WHERE device_id=?", (device_id,))

    # adoption

    def create_adoption(self, req: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO adoption_requests(id, device_id, code, hardware, firmware, ip,
                                                 created_at, expires_at, state)
                   VALUES(:id, :device_id, :code, :hardware, :firmware, :ip, :created_at, :expires_at, 'pending')""",
                req,
            )

    def get_adoption(self, request_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM adoption_requests WHERE id=?", (request_id,)).fetchone())

    def pending_adoptions(self, now: float) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM adoption_requests WHERE state='pending' AND expires_at>? ORDER BY created_at",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_adoption_for(self, device_id: str, now: float) -> dict[str, Any] | None:
        with self._conn() as c:
            return _row(c.execute(
                "SELECT * FROM adoption_requests WHERE device_id=? AND state='pending' AND expires_at>? "
                "ORDER BY created_at DESC LIMIT 1",
                (device_id, now),
            ).fetchone())

    def add_adoption_attempt(self, request_id: str) -> int:
        with self._conn() as c:
            c.execute("UPDATE adoption_requests SET attempts=attempts + 1 WHERE id=?", (request_id,))
            return c.execute("SELECT attempts FROM adoption_requests WHERE id=?", (request_id,)).fetchone()[0]

    def set_adoption_state(self, request_id: str, state: str, token: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE adoption_requests SET state=?, token=? WHERE id=?", (state, token, request_id))

    def collect_adoption_token(self, request_id: str) -> str | None:
        """Hand out the token of an approved request exactly once."""
        with self._conn() as c:
            row = c.execute(
                "SELECT token FROM adoption_requests WHERE id=? AND state='approved'", (request_id,)
            ).fetchone()
            if row is None:
                return None
            c.execute("UPDATE adoption_requests SET state='collected', token=NULL WHERE id=?", (request_id,))
            return row["token"]

    def purge_adoptions(self, older_than: float) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM adoption_requests WHERE expires_at<?", (older_than,))

    # logs

    def add_logs(self, device_id: str, lines: list[tuple[float, str, str, str]], keep: int) -> None:
        with self._conn() as c:
            c.executemany(
                "INSERT INTO logs(device_id, ts, level, tag, message) VALUES(?, ?, ?, ?, ?)",
                [(device_id, *line) for line in lines],
            )
            c.execute(
                "DELETE FROM logs WHERE device_id=? AND id <= "
                "(SELECT id FROM logs WHERE device_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (device_id, device_id, keep),
            )

    def get_logs(self, device_id: str, limit: int = 200, before_id: int | None = None) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, ts, level, tag, message FROM logs WHERE device_id=? AND id<? ORDER BY id DESC LIMIT ?",
                (device_id, before_id or 2**62, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # recordings

    def add_recording(self, rec: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO recordings(id, device_id, created_at, kind, filename, content_type, size) "
                "VALUES(:id, :device_id, :created_at, :kind, :filename, :content_type, :size)",
                rec,
            )

    def trim_recordings(self, device_id: str, keep: int) -> list[str]:
        """Forget all but the newest `keep` recordings of a device. Returns the files to delete."""
        with self._conn() as c:
            rows = c.execute("SELECT id, filename FROM recordings WHERE device_id=? ORDER BY created_at DESC "
                             "LIMIT -1 OFFSET ?", (device_id, max(keep, 0))).fetchall()
            c.executemany("DELETE FROM recordings WHERE id=?", [(r["id"],) for r in rows])
        return [r["filename"] for r in rows]

    def list_recordings(self, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM recordings WHERE device_id=? ORDER BY created_at DESC LIMIT ?", (device_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recording(self, recording_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone())
