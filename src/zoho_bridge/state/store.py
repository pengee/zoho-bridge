"""SQLite-backed state for the bridge.

The store is the durable source of truth for everything that must survive
restarts: folder identity (``mailbox_id``/``uid_validity``), UID assignments
(IMAP UIDs may never be re-used or re-ordered), cached raw message bodies,
sent-copy dedupe keys, and the outbox of pending Zoho API write operations.

Concurrency model: one connection, synchronous calls from the asyncio event
loop (all operations are sub-millisecond on local storage; WAL mode). A
threading lock guards against the rare cross-thread use.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace

__all__ = ['Store', 'FolderRow', 'MessageRow', 'OpRow']

MAX_OP_ATTEMPTS = 10


@dataclass(frozen=True)
class FolderRow:
    zoho_id: str
    imap_name: str
    zoho_name: str
    mailbox_id: str
    uid_validity: int
    uid_next: int = 1


@dataclass(frozen=True)
class MessageRow:
    zoho_folder_id: str
    uid: int
    zoho_id: str | None = None
    message_id: str | None = None
    flags: frozenset[str] = frozenset()
    internal_date: float = 0.0
    size: int | None = None
    subject: str | None = None
    sender: str | None = None
    has_raw: bool = False
    pending_op: str | None = None

    def with_flags(self, flags: frozenset[str]) -> 'MessageRow':
        return replace(self, flags=frozenset(flags))


@dataclass(frozen=True)
class OpRow:
    op_id: int
    kind: str
    zoho_folder_id: str | None
    uid: int | None
    payload: dict
    attempts: int


def normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().strip('<>').lower() or None


def new_mailbox_id() -> str:
    return uuid.uuid4().hex


def new_uid_validity() -> int:
    # same epoch-second convention as pymap's MailboxSnapshot
    return int(time.time()) & 0xFFFFFFFF


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    zoho_id TEXT PRIMARY KEY,
    imap_name TEXT NOT NULL UNIQUE,
    zoho_name TEXT NOT NULL,
    mailbox_id TEXT NOT NULL,
    uid_validity INTEGER NOT NULL,
    uid_next INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS messages (
    zoho_folder_id TEXT NOT NULL,
    uid INTEGER NOT NULL,
    zoho_id TEXT,
    message_id TEXT,
    flags TEXT NOT NULL DEFAULT '',
    internal_date REAL NOT NULL DEFAULT 0,
    size INTEGER,
    subject TEXT,
    sender TEXT,
    raw BLOB,
    raw_last_access REAL,
    pending_op TEXT,
    PRIMARY KEY (zoho_folder_id, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_zoho_id ON messages(zoho_id);
CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id);
CREATE TABLE IF NOT EXISTS outbox_ops (
    op_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    zoho_folder_id TEXT,
    uid INTEGER,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    next_try REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_ops_due ON outbox_ops(status, next_try);
"""


class Store:
    def __init__(self, path: str) -> None:
        if path != ':memory:':
            # ensure parent directory exists for path like /data/bridge.db
            import os
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=NORMAL')
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            # one-shot boot scan; kept exact via deltas afterwards so the
            # hot paths (cache size check, prune) never rescan the BLOBs
            row = self._conn.execute(
                'SELECT COALESCE(SUM(LENGTH(raw)), 0) FROM messages '
                'WHERE raw IS NOT NULL').fetchone()
            self._raw_bytes = int(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meta -----------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO meta(key, value) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (key, value))

    # -- folders ----------------------------------------------------------

    def upsert_folder(self, *, zoho_id: str, imap_name: str,
                      zoho_name: str | None = None,
                      mailbox_id: str | None = None,
                      uid_validity: int | None = None) -> FolderRow:
        """Insert the folder if new; otherwise only refresh the zoho id/name
        mapping (never the identity values)."""
        zoho_name = zoho_name or imap_name
        with self._lock, self._conn:
            row = self._conn.execute(
                'SELECT * FROM folders WHERE imap_name = ?',
                (imap_name,)).fetchone()
            if row is None:
                self._conn.execute(
                    'INSERT INTO folders(zoho_id, imap_name, zoho_name, '
                    'mailbox_id, uid_validity) VALUES(?,?,?,?,?)',
                    (zoho_id, imap_name, zoho_name,
                     mailbox_id or new_mailbox_id(),
                     uid_validity or new_uid_validity()))
            else:
                self._conn.execute(
                    'UPDATE folders SET zoho_id = ?, zoho_name = ? '
                    'WHERE imap_name = ?',
                    (zoho_id, zoho_name, imap_name))
        return self.get_folder_by_name(imap_name)  # type: ignore[return-value]

    def list_folders(self) -> list[FolderRow]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM folders ORDER BY imap_name').fetchall()
        return [_folder_row(r) for r in rows]

    def get_folder_by_name(self, imap_name: str) -> FolderRow | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM folders WHERE imap_name = ?',
                (imap_name,)).fetchone()
        return _folder_row(row) if row else None

    def get_folder_by_zoho_id(self, zoho_id: str) -> FolderRow | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM folders WHERE zoho_id = ?',
                (zoho_id,)).fetchone()
        return _folder_row(row) if row else None

    def delete_folder(self, zoho_id: str) -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                'SELECT COALESCE(SUM(LENGTH(raw)), 0) FROM messages '
                'WHERE zoho_folder_id = ? AND raw IS NOT NULL',
                (zoho_id,)).fetchone()
            self._raw_bytes -= int(row[0])
            self._conn.execute('DELETE FROM messages '
                               'WHERE zoho_folder_id = ?', (zoho_id,))
            self._conn.execute('DELETE FROM folders WHERE zoho_id = ?',
                               (zoho_id,))

    # -- messages -----------------------------------------------------------

    def alloc_uids(self, zoho_folder_id: str, count: int = 1) -> list[int]:
        """Atomically allocate ``count`` new UIDs; returns them in order."""
        with self._lock, self._conn:
            row = self._conn.execute(
                'SELECT uid_next FROM folders WHERE zoho_id = ?',
                (zoho_folder_id,)).fetchone()
            if row is None:
                raise KeyError(zoho_folder_id)
            start = int(row['uid_next'])
            self._conn.execute(
                'UPDATE folders SET uid_next = ? WHERE zoho_id = ?',
                (start + count, zoho_folder_id))
        return list(range(start, start + count))

    def load_messages(self, zoho_folder_id: str) -> list[MessageRow]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT zoho_folder_id, uid, zoho_id, message_id, flags, '
                'internal_date, size, subject, sender, '
                '(raw IS NOT NULL) AS has_raw, pending_op '
                'FROM messages WHERE zoho_folder_id = ? ORDER BY uid',
                (zoho_folder_id,)).fetchall()
        return [_message_row(r) for r in rows]

    def get_message(self, zoho_folder_id: str, uid: int) -> MessageRow | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT zoho_folder_id, uid, zoho_id, message_id, flags, '
                'internal_date, size, subject, sender, '
                '(raw IS NOT NULL) AS has_raw, pending_op '
                'FROM messages WHERE zoho_folder_id = ? AND uid = ?',
                (zoho_folder_id, uid)).fetchone()
        return _message_row(row) if row else None

    def upsert_message(self, row: MessageRow) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO messages(zoho_folder_id, uid, zoho_id, '
                'message_id, flags, internal_date, size, subject, sender, '
                'pending_op) VALUES(?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(zoho_folder_id, uid) DO UPDATE SET '
                'zoho_id=excluded.zoho_id, message_id=excluded.message_id, '
                'flags=excluded.flags, internal_date=excluded.internal_date, '
                'size=excluded.size, subject=excluded.subject, '
                'sender=excluded.sender, pending_op=excluded.pending_op',
                (row.zoho_folder_id, row.uid, row.zoho_id, row.message_id,
                 ' '.join(sorted(row.flags)), row.internal_date, row.size,
                 row.subject, row.sender, row.pending_op))

    def set_flags(self, zoho_folder_id: str, uid: int,
                  flags: frozenset[str]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'UPDATE messages SET flags = ? '
                'WHERE zoho_folder_id = ? AND uid = ?',
                (' '.join(sorted(flags)), zoho_folder_id, uid))

    def set_zoho_id(self, zoho_folder_id: str, uid: int,
                    zoho_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'UPDATE messages SET zoho_id = ? '
                'WHERE zoho_folder_id = ? AND uid = ?',
                (zoho_id, zoho_folder_id, uid))

    def set_pending_op(self, zoho_folder_id: str, uid: int,
                       pending_op: str | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'UPDATE messages SET pending_op = ? '
                'WHERE zoho_folder_id = ? AND uid = ?',
                (pending_op, zoho_folder_id, uid))

    def delete_messages(self, zoho_folder_id: str, uids: list[int]) -> None:
        if not uids:
            return
        with self._lock, self._conn:
            rows = self._conn.execute(
                'SELECT uid, COALESCE(LENGTH(raw), 0) AS len FROM messages '
                'WHERE zoho_folder_id = ? AND raw IS NOT NULL',
                (zoho_folder_id,)).fetchall()
            doomed = set(uids)
            self._raw_bytes -= sum(int(r['len']) for r in rows
                                   if r['uid'] in doomed)
            self._conn.executemany(
                'DELETE FROM messages WHERE zoho_folder_id = ? AND uid = ?',
                [(zoho_folder_id, uid) for uid in uids])

    def find_by_message_id(self, message_id: str | None) \
            -> list[MessageRow]:
        key = normalize_message_id(message_id)
        if not key:
            return []
        with self._lock:
            rows = self._conn.execute(
                'SELECT zoho_folder_id, uid, zoho_id, message_id, flags, '
                'internal_date, size, subject, sender, '
                '(raw IS NOT NULL) AS has_raw, pending_op '
                'FROM messages WHERE message_id = ?', (key,)).fetchall()
        return [_message_row(r) for r in rows]

    # -- raw body cache ------------------------------------------------------

    def store_raw(self, zoho_folder_id: str, uid: int, raw: bytes) -> None:
        new_len = len(raw)
        with self._lock, self._conn:
            prev = self._conn.execute(
                'SELECT COALESCE(LENGTH(raw), 0) FROM messages '
                'WHERE zoho_folder_id = ? AND uid = ?',
                (zoho_folder_id, uid)).fetchone()
            if prev is None:
                return  # no such row; the UPDATE below would no-op anyway
            self._conn.execute(
                'UPDATE messages SET raw = ?, raw_last_access = ? '
                'WHERE zoho_folder_id = ? AND uid = ?',
                (sqlite3.Binary(raw), time.time(), zoho_folder_id, uid))
            self._raw_bytes += new_len - int(prev[0])

    def load_raw(self, zoho_folder_id: str, uid: int) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT raw FROM messages WHERE zoho_folder_id = ? '
                'AND uid = ?', (zoho_folder_id, uid)).fetchone()
        if row is None or row['raw'] is None:
            return None
        with self._lock, self._conn:
            self._conn.execute(
                'UPDATE messages SET raw_last_access = ? '
                'WHERE zoho_folder_id = ? AND uid = ?',
                (time.time(), zoho_folder_id, uid))
        return bytes(row['raw'])

    def raw_cache_size(self) -> int:
        """Total cached raw-body bytes (maintained counter — no table scan)."""
        with self._lock:
            return self._raw_bytes

    def prune_raw_cache(self, max_bytes: int) -> int:
        """Evict least-recently-accessed raw bodies until under budget.
        Returns bytes freed."""
        freed_total = 0
        while True:
            with self._lock, self._conn:
                if self._raw_bytes <= max_bytes:
                    break
                rows = self._conn.execute(
                    'SELECT zoho_folder_id, uid, LENGTH(raw) AS len '
                    'FROM messages WHERE raw IS NOT NULL AND pending_op '
                    'IS NULL ORDER BY raw_last_access ASC LIMIT 2000'
                ).fetchall()
                if not rows:
                    break
                target = self._raw_bytes - max_bytes
                doomed: list[tuple[str, int]] = []
                page_freed = 0
                for r in rows:
                    doomed.append((r['zoho_folder_id'], int(r['uid'])))
                    page_freed += int(r['len'] or 0)
                    if page_freed >= target:
                        break
                if not doomed or page_freed == 0:
                    break  # nothing left to free (all zero-length)
                self._conn.executemany(
                    'UPDATE messages SET raw = NULL, raw_last_access = NULL '
                    'WHERE zoho_folder_id = ? AND uid = ?', doomed)
                self._raw_bytes -= page_freed
                freed_total += page_freed
        return freed_total

    # -- outbox ops -----------------------------------------------------------

    def enqueue_op(self, kind: str, *, zoho_folder_id: str | None = None,
                   uid: int | None = None, payload: dict | None = None,
                   delay: float = 0.0) -> int:
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                'INSERT INTO outbox_ops(kind, zoho_folder_id, uid, payload, '
                'created, next_try) VALUES(?,?,?,?,?,?)',
                (kind, zoho_folder_id, uid, _dumps(payload or {}),
                 now, now + delay))
            return int(cur.lastrowid)

    def pending_uids(self, zoho_folder_id: str,
                     kinds: tuple[str, ...] = ('flag_read', 'flag_star')) \
            -> set[int]:
        """UIDs in the folder with pending outbox ops of the given kinds.

        The poller must not reconcile flags/expunges for these rows — the
        Zoho state is stale until the ops worker drains them.
        """
        placeholders = ','.join('?' * len(kinds))
        with self._lock:
            rows = self._conn.execute(
                f'SELECT DISTINCT uid FROM outbox_ops WHERE status = ? AND '
                f'zoho_folder_id = ? AND kind IN ({placeholders})',
                ('pending', zoho_folder_id, *kinds)).fetchall()
        return {int(r['uid']) for r in rows if r['uid'] is not None}

    def pending_zoho_ids(self, zoho_folder_id: str,
                         kind: str = 'expunge') -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT payload FROM outbox_ops WHERE status = ? AND '
                'zoho_folder_id IS NULL AND kind = ?',
                ('pending', kind)).fetchall()
        ids: set[str] = set()
        for row in rows:
            payload = _loads(row['payload'])
            ids.update(payload.get('zoho_ids') or [])
        return ids

    def pop_due_ops(self, limit: int = 50) -> list[OpRow]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM outbox_ops WHERE status = ? AND next_try <= ? '
                'ORDER BY next_try LIMIT ?', ('pending', now, limit)).fetchall()
        return [OpRow(op_id=int(r['op_id']), kind=r['kind'],
                      zoho_folder_id=r['zoho_folder_id'],
                      uid=int(r['uid']) if r['uid'] is not None else None,
                      payload=_loads(r['payload']),
                      attempts=int(r['attempts'])) for r in rows]

    def op_done(self, op_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM outbox_ops WHERE op_id = ?',
                               (op_id,))

    def op_fail(self, op_id: int, error: str, delay: float = 60.0) -> bool:
        """Record a failure; returns False when the op is dead (gave up)."""
        with self._lock, self._conn:
            row = self._conn.execute(
                'SELECT attempts FROM outbox_ops WHERE op_id = ?',
                (op_id,)).fetchone()
            if row is None:
                return False
            attempts = int(row['attempts']) + 1
            if attempts >= MAX_OP_ATTEMPTS:
                self._conn.execute(
                    "UPDATE outbox_ops SET status = 'dead' WHERE op_id = ?",
                    (op_id,))
                return False
            self._conn.execute(
                'UPDATE outbox_ops SET attempts = ?, next_try = ? '
                'WHERE op_id = ?',
                (attempts, time.time() + delay, op_id))
        return True

    def op_counts(self) -> tuple[int, int]:
        """Return ``(pending, dead)`` outbox op counts."""
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(status = 'pending') AS pending, "
                "SUM(status = 'dead') AS dead FROM outbox_ops").fetchone()
        return int(row['pending'] or 0), int(row['dead'] or 0)


def _dumps(payload: dict) -> str:
    return json.dumps(payload)


def _loads(raw: str) -> dict:
    try:
        val = json.loads(raw)
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def _folder_row(r: sqlite3.Row) -> FolderRow:
    return FolderRow(zoho_id=r['zoho_id'], imap_name=r['imap_name'],
                     zoho_name=r['zoho_name'], mailbox_id=r['mailbox_id'],
                     uid_validity=int(r['uid_validity']),
                     uid_next=int(r['uid_next']))


def _message_row(r: sqlite3.Row) -> MessageRow:
    return MessageRow(
        zoho_folder_id=r['zoho_folder_id'], uid=int(r['uid']),
        zoho_id=r['zoho_id'], message_id=normalize_message_id(r['message_id']),
        flags=frozenset((r['flags'] or '').split()),
        internal_date=float(r['internal_date'] or 0.0),
        size=r['size'], subject=r['subject'], sender=r['sender'],
        has_raw=bool(r['has_raw']), pending_op=r['pending_op'])
