"""Direct unit tests for poller reconciliation paths (stub Zoho client).

These exercise paths the full E2E topology cannot reach deterministically:
an APPENDed sent copy that is only linked to its Zoho twin on a later sync
(the APPEND-before-sync ordering races the poller). Regression guard for the
bug where the store row was linked but the live in-memory Message kept
zoho_id=None, so flag/expunge ops on the copy silently stopped propagating.
"""

from __future__ import annotations

import time

from zoho_bridge.config import BridgeSettings
from zoho_bridge.imap.backend import BridgeContext, ZohoMailboxData
from zoho_bridge.poller import Poller
from zoho_bridge.state.store import MessageRow, Store
from zoho_bridge.zoho.models import MessageSummary

SENT_RAW = (b'From: sender@example.com\r\n'
            b'To: me@example.com\r\n'
            b'Subject: Linked sent copy\r\n'
            b'Message-ID: <link-test@example.com>\r\n'
            b'Content-Type: text/plain; charset=utf-8\r\n'
            b'\r\nbody\r\n')


class StubClient:
    def __init__(self, summaries: list[MessageSummary],
                 raw: bytes) -> None:
        self.summaries = summaries
        self.raw = raw
        self.fetched: list[str] = []

    async def list_messages(self, folder_id: str, *, limit: int = 50,
                            start: int = 0) -> list[MessageSummary]:
        return self.summaries[start:start + limit]

    async def fetch_original(self, message_id: str) -> bytes:
        self.fetched.append(message_id)
        return self.raw


async def test_sent_copy_link_refreshes_in_memory_message() -> None:
    """Linking a synced Zoho copy to an APPENDed local row must update both
    the store row and the live Message object."""
    store = Store(':memory:')
    folder = store.upsert_folder(zoho_id='77', imap_name='Sent',
                                 zoho_name='Sent')
    # the locally APPENDed copy: no zoho_id, raw present, Message-ID known
    uid = store.alloc_uids('77')[0]
    store.upsert_message(MessageRow(
        zoho_folder_id='77', uid=uid,
        message_id='link-test@example.com',
        internal_date=time.time(), subject='Linked sent copy'))
    store.store_raw('77', uid, SENT_RAW)

    summary = MessageSummary(zoho_id='999', subject='Linked sent copy',
                             received_epoch=time.time(), read=True)
    context = BridgeContext(BridgeSettings(prefetch=0),
                            StubClient([summary], SENT_RAW), store)
    data = ZohoMailboxData(context, folder)
    assert data._messages[uid].zoho_id is None  # noqa: SLF001

    await Poller(context).sync_folder(data)

    assert store.get_message('77', uid).zoho_id == '999'
    assert data._messages[uid].zoho_id == '999'  # noqa: SLF001
    # no second, duplicate message was added to the mailbox
    assert len(data._messages) == 1  # noqa: PLR2004


async def test_remote_deletion_purges_store_and_memory() -> None:
    """A message that disappeared on Zoho must be dropped from the store
    (not just memory), or it resurrects on every restart."""
    store = Store(':memory:')
    folder = store.upsert_folder(zoho_id='77', imap_name='INBOX',
                                 zoho_name='Inbox')
    uid = store.alloc_uids('77')[0]
    store.upsert_message(MessageRow(zoho_folder_id='77', uid=uid,
                                    zoho_id='gone',
                                    internal_date=time.time()))
    context = BridgeContext(BridgeSettings(prefetch=0),
                            StubClient([], b''), store)
    data = ZohoMailboxData(context, folder)
    assert len(data._messages) == 1  # noqa: PLR2004

    await Poller(context).sync_folder(data)

    assert store.get_message('77', uid) is None
    assert len(data._messages) == 0
