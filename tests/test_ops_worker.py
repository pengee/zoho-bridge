"""Unit tests for the OpsWorker outbox drain semantics.

Guards the retry/backoff fix (failures must back off by attempt count, not
a flat 60 s) and the pending-marker contract: every terminal path of
``upload_draft``/``file_sent`` clears ``pending_op``, while an API error
keeps the marker set so the retried op proceeds.
"""

from __future__ import annotations

import time

import pytest

from zoho_bridge.config import BridgeSettings
from zoho_bridge.imap.backend import BridgeContext
from zoho_bridge.poller import OpsWorker
from zoho_bridge.state.store import MessageRow, Store
from zoho_bridge.zoho.client import ZohoApiError

DRAFT_RAW = (b'Subject: my draft\r\nFrom: me@example.com\r\n'
             b'Message-ID: <d1@example.com>\r\n'
             b'Content-Type: text/plain; charset=utf-8\r\n\r\nhi\r\n')


class StubClient:
    def __init__(self, *, draft_id: str | None = '888',
                 fail: bool = False) -> None:
        self.draft_id = draft_id
        self.fail = fail
        self.saved: list[dict] = []
        self.moved: list[tuple[list[str], str]] = []

    async def save_draft(self, payload: dict) -> str | None:
        if self.fail:
            raise ZohoApiError('POST', '/messages', 500, 'boom')
        self.saved.append(payload)
        return self.draft_id

    async def move_messages(self, message_ids: list[str], *,
                            to_folder_id: str,
                            from_folder_id: str | None = None) -> bool:
        self.moved.append((message_ids, to_folder_id))
        return True


def _make_store(folder_name: str = 'Drafts') -> Store:
    store = Store(':memory:')
    store.upsert_folder(zoho_id='77', imap_name=folder_name,
                        zoho_name=folder_name)
    return store


def _worker(store: Store, client: StubClient | None = None) -> OpsWorker:
    context = BridgeContext(BridgeSettings(),
                            client or StubClient(), store)
    return OpsWorker(context)


def _op(store: Store, kind: str, **kw: object):
    op_id = store.enqueue_op(kind, **kw)  # type: ignore[arg-type]
    return store.pop_due_ops()[0] if op_id else None


def _get_op_row(store: Store, op_id: int):
    with store._lock:  # noqa: SLF001 - test introspection
        return store._conn.execute(  # noqa: SLF001
            'SELECT * FROM outbox_ops WHERE op_id = ?', (op_id,)).fetchone()


# -- backoff (B3) ---------------------------------------------------------

def test_fail_backs_off_by_attempt_count() -> None:
    store = _make_store()
    worker = _worker(store)
    op_id = store.enqueue_op('expunge',
                             payload={'zoho_ids': ['1'], 'folder_id': '77'})
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            'UPDATE outbox_ops SET attempts = 3 WHERE op_id = ?', (op_id,))
        store._conn.commit()  # noqa: SLF001
    op = store.pop_due_ops()[0]
    assert op.attempts == 3
    worker._fail(op, ZohoApiError('DELETE', '/x', 500, 'boom'))
    row = _get_op_row(store, op_id)
    assert row['status'] == 'pending'
    delay = row['next_try'] - time.time()
    # attempts becomes 4 → 60*4 = 240 s (pre-fix this stayed at the flat
    # 60 s default of op_fail)
    assert 230 < delay <= 241


def test_fail_dead_after_max_attempts_clears_marker() -> None:
    store = _make_store()
    store.upsert_message(MessageRow(zoho_folder_id='77', uid=5,
                                    pending_op='upload_draft'))
    worker = _worker(store)
    op_id = store.enqueue_op('upload_draft', zoho_folder_id='77', uid=5,
                             payload={})
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            'UPDATE outbox_ops SET attempts = 9 WHERE op_id = ?', (op_id,))
        store._conn.commit()  # noqa: SLF001
    op = store.pop_due_ops()[0]
    worker._fail(op, ZohoApiError('POST', '/x', 500, 'boom'))
    assert _get_op_row(store, op_id)['status'] == 'dead'
    assert store.get_message('77', 5).pending_op is None


# -- draft upload marker contract (B4) ------------------------------------

async def test_upload_draft_happy_path_clears_marker_and_links() -> None:
    store = _make_store()
    uid = store.alloc_uids('77')[0]
    store.upsert_message(MessageRow(zoho_folder_id='77', uid=uid,
                                    pending_op='upload_draft'))
    store.store_raw('77', uid, DRAFT_RAW)
    client = StubClient(draft_id='888')
    worker = _worker(store, client)
    op = _op(store, 'upload_draft', zoho_folder_id='77', uid=uid,
             payload={})
    await worker.apply(op)
    row = store.get_message('77', uid)
    assert row.zoho_id == '888'
    assert row.pending_op is None
    assert len(client.saved) == 1


async def test_upload_draft_missing_raw_clears_marker() -> None:
    store = _make_store()
    uid = store.alloc_uids('77')[0]
    store.upsert_message(MessageRow(zoho_folder_id='77', uid=uid,
                                    pending_op='upload_draft'))
    client = StubClient()
    worker = _worker(store, client)
    op = _op(store, 'upload_draft', zoho_folder_id='77', uid=uid,
             payload={})
    await worker.apply(op)
    assert store.get_message('77', uid).pending_op is None
    assert client.saved == []


async def test_upload_draft_api_error_keeps_marker_for_retry() -> None:
    store = _make_store()
    uid = store.alloc_uids('77')[0]
    store.upsert_message(MessageRow(zoho_folder_id='77', uid=uid,
                                    pending_op='upload_draft'))
    store.store_raw('77', uid, DRAFT_RAW)
    worker = _worker(store, StubClient(fail=True))
    op = _op(store, 'upload_draft', zoho_folder_id='77', uid=uid,
             payload={})
    with pytest.raises(ZohoApiError):
        await worker.apply(op)
    # marker stays set while the op is alive for retry
    assert store.get_message('77', uid).pending_op == 'upload_draft'


# -- sent filing marker contract (B4) -------------------------------------

async def test_file_sent_clears_marker_when_already_linked() -> None:
    store = _make_store('Sent')
    uid = store.alloc_uids('77')[0]
    # poller linked the Zoho copy while the op was queued
    store.upsert_message(MessageRow(zoho_folder_id='77', uid=uid,
                                    zoho_id='555',
                                    pending_op='file_sent'))
    store.store_raw('77', uid, DRAFT_RAW)
    client = StubClient()
    worker = _worker(store, client)
    op = _op(store, 'file_sent', zoho_folder_id='77', uid=uid,
             payload={})
    await worker.apply(op)
    assert store.get_message('77', uid).pending_op is None
    assert client.saved == []


async def test_file_sent_uploads_and_moves_when_unfiled() -> None:
    store = _make_store('Sent')
    uid = store.alloc_uids('77')[0]
    store.upsert_message(MessageRow(zoho_folder_id='77', uid=uid,
                                    message_id='d1@example.com',
                                    pending_op='file_sent'))
    store.store_raw('77', uid, DRAFT_RAW)
    client = StubClient(draft_id='999')
    worker = _worker(store, client)
    op = _op(store, 'file_sent', zoho_folder_id='77', uid=uid,
             payload={})
    await worker.apply(op)
    row = store.get_message('77', uid)
    assert row.zoho_id == '999'
    assert row.pending_op is None
    assert client.saved and client.moved
