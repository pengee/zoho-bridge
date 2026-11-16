"""Unit tests for the SQLite store and Zoho response normalization."""

import sqlite3

from zoho_bridge.state.store import (
    MAX_OP_ATTEMPTS,
    MessageRow,
    Store,
    normalize_message_id,
)
from zoho_bridge.zoho.models import MessageSummary

# schema exactly as shipped before the `deleted` column was dropped —
# a database created by that version must keep working untouched
_LEGACY_SCHEMA = """
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
    uid_next INTEGER NOT NULL DEFAULT 1,
    deleted INTEGER NOT NULL DEFAULT 0
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


def test_legacy_schema_database_still_opens(tmp_path) -> None:
    """Databases created by the pre-cleanup version (extra `deleted`
    column) must open, read, write and bootstrap the raw counter."""
    path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO folders(zoho_id, imap_name, zoho_name, mailbox_id, "
        "uid_validity, uid_next, deleted) "
        "VALUES('F1', 'INBOX', 'Inbox', 'mb1', 1, 7, 0)")
    conn.execute("INSERT INTO messages(zoho_folder_id, uid, zoho_id, raw) "
                 "VALUES('F1', 1, 'M1', x'616263')")
    conn.commit()
    conn.close()

    store = Store(str(path))
    try:
        assert store.get_folder_by_name('INBOX').zoho_id == 'F1'
        assert store.alloc_uids('F1') == [7]
        assert store.raw_cache_size() == 3  # counter bootstrapped
        assert store.load_raw('F1', 1) == b'abc'
        # inserts no longer name the legacy column — default covers it
        store.upsert_folder(zoho_id='F2', imap_name='Sent')
        assert store.get_folder_by_name('Sent').zoho_id == 'F2'
        # last_error/failed_at came from the boot migration — op_fail
        # writes them, so the pending UPDATE proves the ALTER happened
        op_id = store.enqueue_op('flag_read', payload={'zoho_id': 'M1'})
        assert store.op_fail(op_id, 'boom', delay=0)
        assert store.recent_dead_ops() == []
    finally:
        store.close()


def test_normalize_message_id() -> None:
    assert normalize_message_id('<AbC@Example.COM>') == 'abc@example.com'
    assert normalize_message_id('  x@y  ') == 'x@y'
    assert normalize_message_id(None) is None
    assert normalize_message_id('') is None


def test_uid_allocation_monotonic(tmp_path) -> None:
    store = Store(str(tmp_path / 't.db'))
    store.upsert_folder(zoho_id='F1', imap_name='INBOX')
    uids = store.alloc_uids('F1', 3)
    assert uids == [1, 2, 3]
    assert store.alloc_uids('F1') == [4]
    assert store.alloc_uids('F1', 2) == [5, 6]


def test_folder_lifecycle(tmp_path) -> None:
    store = Store(str(tmp_path / 't.db'))
    row = store.upsert_folder(zoho_id='F1', imap_name='INBOX',
                              zoho_name='Inbox')
    assert row.imap_name == 'INBOX' and row.zoho_name == 'Inbox'
    mailbox_id = row.mailbox_id
    uid_validity = row.uid_validity
    # re-upsert keeps identity, refreshes zoho id
    row2 = store.upsert_folder(zoho_id='F2', imap_name='INBOX',
                               zoho_name='Inbox')
    assert row2.mailbox_id == mailbox_id
    assert row2.uid_validity == uid_validity
    assert row2.zoho_id == 'F2'
    assert store.get_folder_by_zoho_id('F2').imap_name == 'INBOX'
    store.delete_folder('F2')
    assert store.get_folder_by_name('INBOX') is None
    assert store.get_folder_by_zoho_id('F2') is None


def test_message_roundtrip_and_flags(tmp_path) -> None:
    store = Store(str(tmp_path / 't.db'))
    store.upsert_folder(zoho_id='F1', imap_name='INBOX')
    uid = store.alloc_uids('F1')[0]
    store.upsert_message(MessageRow(
        zoho_folder_id='F1', uid=uid, zoho_id='M1',
        message_id='<a@b>', flags=frozenset({'\\Seen'}),
        internal_date=123.0, subject='Hi', sender='x@y'))
    row = store.get_message('F1', uid)
    assert row.zoho_id == 'M1'
    assert row.flags == {'\\Seen'}
    assert row.message_id == 'a@b'
    assert row.subject == 'Hi'
    # flag update
    store.set_flags('F1', uid, frozenset())
    assert store.get_message('F1', uid).flags == set()
    # zoho id link
    store.set_zoho_id('F1', uid, 'M9')
    assert store.get_message('F1', uid).zoho_id == 'M9'
    # delete
    store.delete_messages('F1', [uid])
    assert store.get_message('F1', uid) is None


def test_raw_cache_and_prune(tmp_path) -> None:
    store = Store(str(tmp_path / 't.db'))
    store.upsert_folder(zoho_id='F1', imap_name='INBOX')
    uid = store.alloc_uids('F1')[0]
    store.upsert_message(MessageRow(zoho_folder_id='F1', uid=uid,
                                    zoho_id='M1'))
    store.store_raw('F1', uid, b'x' * 1000)
    assert store.raw_cache_size() == 1000
    assert store.load_raw('F1', uid) == b'x' * 1000
    freed = store.prune_raw_cache(max_bytes=0)
    assert freed == 1000
    assert store.load_raw('F1', uid) is None


def test_raw_cache_counter_tracks_all_mutations(tmp_path) -> None:
    """The O(1) counter must equal the real BLOB sum across store/update/
    delete/folder-delete, and be re-derived from disk on reopen."""
    path = str(tmp_path / 't.db')
    store = Store(path)
    store.upsert_folder(zoho_id='F1', imap_name='INBOX')
    store.upsert_folder(zoho_id='F2', imap_name='Sent')
    u1 = store.alloc_uids('F1')[0]
    u2 = store.alloc_uids('F1')[0]
    u3 = store.alloc_uids('F2')[0]
    for uid, fid in ((u1, 'F1'), (u2, 'F1'), (u3, 'F2')):
        store.upsert_message(MessageRow(zoho_folder_id=fid, uid=uid))
    store.store_raw('F1', u1, b'a' * 100)
    store.store_raw('F1', u2, b'b' * 200)
    store.store_raw('F2', u3, b'c' * 300)
    assert store.raw_cache_size() == 600
    store.store_raw('F1', u1, b'a' * 50)  # overwrite shrinks
    assert store.raw_cache_size() == 550
    store.delete_messages('F1', [u2])
    assert store.raw_cache_size() == 350
    store.close()
    reopened = Store(path)
    assert reopened.raw_cache_size() == 350  # boot scan re-derives
    reopened.delete_folder('F2')
    assert reopened.raw_cache_size() == 50   # folder raw purged from count
    reopened.close()


def test_prune_frees_only_what_is_needed(tmp_path) -> None:
    store = Store(str(tmp_path / 't.db'))
    store.upsert_folder(zoho_id='F1', imap_name='INBOX')
    uids = store.alloc_uids('F1', 3)
    for uid in uids:
        store.upsert_message(MessageRow(zoho_folder_id='F1', uid=uid))
        store.store_raw('F1', uid, b'x' * 100)
    assert store.raw_cache_size() == 300
    freed = store.prune_raw_cache(max_bytes=200)
    assert freed == 100  # one victim suffices
    assert store.raw_cache_size() == 200


def test_ops_lifecycle(tmp_path) -> None:
    store = Store(str(tmp_path / 't.db'))
    op_id = store.enqueue_op('flag_read', zoho_folder_id='F1', uid=1,
                             payload={'zoho_id': 'M1', 'read': True})
    ops = store.pop_due_ops()
    assert len(ops) == 1 and ops[0].op_id == op_id
    assert ops[0].payload['read'] is True
    store.op_done(op_id)
    assert store.pop_due_ops() == []
    # failures eventually dead
    op_id = store.enqueue_op('expunge', payload={'zoho_ids': ['M1']})
    for _ in range(20):
        if not store.op_fail(op_id, 'boom', delay=0):
            break
    ops = store.pop_due_ops()
    assert ops == []  # dead ops are never popped again


def test_op_fail_records_error_and_dead_ops_are_listable(tmp_path) -> None:
    store = Store(str(tmp_path / 't.db'))
    try:
        # a retried failure keeps the flattened reason while still pending
        op_id = store.enqueue_op('flag_read', payload={'zoho_id': 'M1',
                                                       'read': True})
        assert store.op_fail(op_id, 'PUT x -> HTTP 200:\r\n RATE\r\nLIMIT',
                             delay=0)
        with store._lock:
            row = store._conn.execute(
                'SELECT last_error, failed_at FROM outbox_ops '
                'WHERE op_id = ?', (op_id,)).fetchone()
        assert row['last_error'] == 'PUT x -> HTTP 200: RATE LIMIT'
        assert row['failed_at']
        assert store.recent_dead_ops() == []

        # driving it to dead records kind + final reason for /status
        alive = True
        while alive:
            alive = store.op_fail(op_id, 'MESSAGE_NOT_FOUND', delay=0)
        assert store.pop_due_ops() == []
        dead = store.recent_dead_ops()
        assert [d['op_id'] for d in dead] == [op_id]
        assert dead[0]['kind'] == 'flag_read'
        assert dead[0]['last_error'] == 'MESSAGE_NOT_FOUND'
        assert dead[0]['attempts'] == MAX_OP_ATTEMPTS

        # newest failure first, limit honored
        op2 = store.enqueue_op('expunge', payload={'zoho_ids': ['M2']})
        alive = True
        while alive:
            alive = store.op_fail(op2, 'gone', delay=0)
        with store._lock, store._conn:
            store._conn.execute(
                'UPDATE outbox_ops SET failed_at = failed_at - 100 '
                'WHERE op_id = ?', (op_id,))
        assert [d['op_id'] for d in store.recent_dead_ops()] == [op2, op_id]
        assert len(store.recent_dead_ops(limit=1)) == 1
    finally:
        store.close()


def test_message_summary_normalization() -> None:
    # real-schema shapes verified by the spike (string epoch-ms, status
    # "1"/"0" for read, flagid "important"/"flag_not_set" for star)
    summary = MessageSummary.from_view({
        'messageId': '1794707492965153000',
        'subject': 'Hello',
        'fromAddress': 'a@b',
        'receivedTime': '1700000000000',
        'size': '123',
        'hasAttachment': 'true',
        'status': '1',
        'status2': '0',
        'flagid': 'important',
    })
    assert summary.zoho_id == '1794707492965153000'
    assert summary.received_epoch == 1700000000.0
    assert summary.read is True
    assert summary.starred is True
    unread = MessageSummary.from_view({'messageId': '1', 'status': '0',
                                       'flagid': 'flag_not_set'})
    assert unread.read is False and unread.starred is False
