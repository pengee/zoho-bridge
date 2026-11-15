"""Background sync: Zoho → bridge (poller) and bridge → Zoho (ops worker).

The poller reconciles the folder list and per-folder message lists from the
Zoho REST API into the store and pushes changes into the live IMAP mailboxes
(waking IDLE waiters). The ops worker drains the outbox of pending Zoho
write operations with retry/backoff.
"""

from __future__ import annotations

import asyncio
import logging
import time
import email.utils
from email import message_from_bytes
from email.message import Message as MimeMessage
from typing import Any

from .state.store import FolderRow, MessageRow
from .zoho.client import ZohoApiError, ZohoClient
from .imap.backend import BridgeContext, ZohoMailboxData

__all__ = ['Poller', 'OpsWorker']

log = logging.getLogger(__name__)

PAGE_SIZE = 50
MAX_PAGES = 200  # safety cap: 10k messages per folder per sync


class Poller:
    def __init__(self, context: BridgeContext) -> None:
        self.context = context

    async def run_forever(self) -> None:
        context = self.context
        while True:
            try:
                await self.sync_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception('sync failed')
            try:
                await asyncio.wait_for(context.sync_wake.wait(),
                                       timeout=context.settings.poll_seconds)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            context.sync_wake.clear()

    # -- top level ---------------------------------------------------------

    async def sync_all(self) -> None:
        context = self.context
        mailbox_set = context.mailbox_set
        if mailbox_set is None:
            return
        try:
            await context.client.get_account()
            folders = await context.client.list_folders()
        except ZohoApiError as exc:
            log.warning('Zoho API unavailable: %s', exc)
            return
        await self._reconcile_folders(folders)
        for data in mailbox_set.all_data():
            try:
                await self.sync_folder(data)
            except ZohoApiError as exc:
                log.warning('sync of %s failed: %s', data.folder.imap_name,
                            exc)
        context.last_sync_epoch = time.time()
        context.sync_ok = True

    # -- folders -------------------------------------------------------------

    async def _reconcile_folders(self, folders: list[Any]) -> None:
        context = self.context
        store = context.store
        mailbox_set = context.mailbox_set
        assert mailbox_set is not None
        seen_zoho_ids: set[str] = set()
        seen_names: set[str] = set()
        for zf in folders:
            zoho_id = zf.zoho_id
            seen_zoho_ids.add(zoho_id)
            imap_name = 'INBOX' if zf.name.casefold() == 'inbox' else zf.name
            seen_names.add(imap_name)
            existing = store.get_folder_by_name(imap_name)
            if existing is not None:
                if existing.zoho_id == zoho_id:
                    continue  # unchanged binding
                # Zoho folder was deleted and re-created — new identity
                store.delete_folder(existing.zoho_id)
                mailbox_set.evict(imap_name)
            row = store.upsert_folder(zoho_id=zoho_id, imap_name=imap_name,
                                      zoho_name=zf.name)
            mailbox_set.add_data(row)
            log.info('discovered folder %r (zoho id %s)', imap_name, zoho_id)
        for row in store.list_folders():
            if row.imap_name in seen_names:
                continue
            log.info('folder %r disappeared on Zoho; removing',
                     row.imap_name)
            store.delete_folder(row.zoho_id)
            mailbox_set.evict(row.imap_name)

    # -- messages -----------------------------------------------------------

    async def sync_folder(self, data: ZohoMailboxData) -> None:
        context = self.context
        store = context.store
        folder = data.folder
        zoho_id = folder.zoho_id

        remote: dict[str, Any] = {}
        start = 0
        for _ in range(MAX_PAGES):
            summaries = await context.client.list_messages(
                zoho_id, limit=PAGE_SIZE, start=start)
            if not summaries:
                break
            for summary in summaries:
                remote[summary.zoho_id] = summary
            if len(summaries) < PAGE_SIZE:
                break
            start += PAGE_SIZE

        existing = {row.zoho_id: row
                    for row in store.load_messages(zoho_id) if row.zoho_id}
        # rows with in-flight local writes: Zoho state is stale for these
        # until the ops worker drains them — never reconcile from Zoho now
        pending_uids = store.pending_uids(zoho_id)
        pending_expunge = store.pending_zoho_ids(zoho_id)
        added_rows: list[MessageRow] = []
        flag_rows: list[MessageRow] = []
        sent_dedupe: list[tuple[MessageRow, Any]] = []
        now = time.time()
        for zoho_msg_id, summary in remote.items():
            if zoho_msg_id in pending_expunge:
                continue  # locally deleted; op in flight
            desired_flags = frozenset(
                f for f, on in (('\\Seen', summary.read),
                                ('\\Flagged', summary.starred)) if on)
            row = existing.get(zoho_msg_id)
            if row is None:
                uid = store.alloc_uids(zoho_id)[0]
                new_row = MessageRow(
                    zoho_folder_id=zoho_id, uid=uid, zoho_id=zoho_msg_id,
                    flags=desired_flags,
                    internal_date=summary.received_epoch or now,
                    size=summary.size, subject=summary.subject,
                    sender=summary.sender)
                if folder.imap_name == 'Sent':
                    sent_dedupe.append((new_row, summary))
                else:
                    store.upsert_message(new_row)
                    added_rows.append(new_row)
            elif row.uid not in pending_uids:
                if desired_flags != row.flags:
                    updated = row.with_flags(desired_flags)
                    store.set_flags(zoho_id, row.uid, desired_flags)
                    flag_rows.append(updated)
        removed = [row.uid for zoho_msg_id, row in existing.items()
                   if zoho_msg_id not in remote and not row.pending_op
                   and row.uid not in pending_uids]

        # Sent folder: link the Zoho copy of a message we already appended
        # locally (Nextcloud Mail saves its sent copy via APPEND).
        for new_row, summary in sent_dedupe:
            zoho_msg_id = new_row.zoho_id
            raw = await context.client.fetch_original(zoho_msg_id)
            msg_id = _extract_message_id(raw)
            linked = None
            if msg_id:
                for candidate in store.find_by_message_id(msg_id):
                    if candidate.zoho_folder_id == zoho_id and \
                            candidate.zoho_id is None and \
                            not candidate.pending_op:
                        linked = candidate
                        break
            if linked is None:
                # Zoho may rewrite Message-IDs — match local rows by subject
                # and receive-time proximity instead.
                for candidate in store.load_messages(zoho_id):
                    if candidate.zoho_id is None and \
                            not candidate.pending_op and \
                            candidate.subject == summary.subject and \
                            abs(candidate.internal_date -
                                (summary.received_epoch or now)) < 600.0:
                        linked = candidate
                        break
            if linked is not None:
                store.set_zoho_id(zoho_id, linked.uid, zoho_msg_id)
                refreshed = store.get_message(zoho_id, linked.uid)
                if refreshed is not None:
                    # also swap the live in-memory Message, otherwise its
                    # zoho_id stays None and flag/expunge ops on this copy
                    # silently stop reaching Zoho until a restart
                    data.refresh_row(refreshed)
                log.debug('linked sent copy %s to local uid %d',
                          zoho_msg_id, linked.uid)
            else:
                store.upsert_message(new_row)
                store.store_raw(zoho_id, new_row.uid, raw)
                added_rows.append(new_row)

        if added_rows or flag_rows or removed:
            log.debug('%s: +%d ~%d -%d', folder.imap_name, len(added_rows),
                      len(flag_rows), len(removed))
            if removed:
                # purge the store rows too, or Zoho-side deletions
                # resurrect from _load_from_store on every restart
                store.delete_messages(zoho_id, removed)
            await data.apply_remote(added_rows, flag_rows, removed)
        await self._prefetch(data, added_rows)

    async def _prefetch(self, data: ZohoMailboxData,
                        added: list[MessageRow]) -> None:
        """Warm the raw-body cache for the newest added messages."""
        context = self.context
        budget = context.settings.prefetch
        if budget <= 0 or not added:
            return
        newest = added[-budget:]
        semaphore = asyncio.Semaphore(4)

        async def fetch(row: MessageRow) -> None:
            if row.zoho_id is None or row.has_raw:
                return
            async with semaphore:
                try:
                    raw = await context.client.fetch_original(row.zoho_id)
                except ZohoApiError as exc:
                    log.debug('prefetch %s failed: %s', row.zoho_id, exc)
                    return
                context.store.store_raw(data.folder.zoho_id, row.uid, raw)

        await asyncio.gather(*(fetch(row) for row in newest))
        try:
            context.store.prune_raw_cache(
                context.settings.raw_cache_mb * 1024 * 1024)
        except Exception:  # noqa: BLE001 - pruning is best-effort
            pass


def _extract_message_id(raw: bytes) -> str | None:
    try:
        msg = message_from_bytes(raw)
    except Exception:  # noqa: BLE001 - malformed message
        return None
    return msg.get('Message-ID')


class OpsWorker:
    """Drains the outbox of pending Zoho write operations."""

    def __init__(self, context: BridgeContext) -> None:
        self.context = context

    async def run_forever(self) -> None:
        while True:
            ops = self.context.store.pop_due_ops(limit=20)
            if not ops:
                await asyncio.sleep(5.0)
                continue
            for op in ops:
                try:
                    await self.apply(op)
                except asyncio.CancelledError:
                    raise
                except ZohoApiError as exc:
                    self._fail(op, exc)
                except Exception as exc:  # noqa: BLE001
                    self._fail(op, exc)
                else:
                    self.context.store.op_done(op.op_id)

    def _fail(self, op: Any, exc: Exception) -> None:
        attempts = op.attempts + 1
        delay = min(60 * attempts, 3600)
        alive = self.context.store.op_fail(op.op_id, str(exc), delay)
        if not alive:
            log.error('op %s %s abandoned after %d attempts: %s',
                      op.op_id, op.kind, attempts, exc)
            self._abandon(op)
        else:
            log.warning('op %s %s failed (attempt %d, retry in %ds): %s',
                        op.op_id, op.kind, attempts, delay, exc)

    def _abandon(self, op: Any) -> None:
        """Give up: clear any pending marker on the message row."""
        if op.zoho_folder_id and op.uid:
            self.context.store.set_pending_op(op.zoho_folder_id, op.uid, None)

    async def apply(self, op: Any) -> None:
        context = self.context
        client: ZohoClient = context.client
        kind = op.kind
        payload = op.payload
        if kind == 'flag_read':
            await client.mark_read([payload['zoho_id']],
                                   read=payload['read'],
                                   folder_id=payload.get('folder_id'))
        elif kind == 'flag_star':
            await client.set_star([payload['zoho_id']],
                                  starred=payload['starred'],
                                  folder_id=payload.get('folder_id'))
        elif kind == 'expunge':
            await client.delete_messages(payload['zoho_ids'],
                                         payload['folder_id'])
        elif kind == 'upload_draft':
            await self._upload_draft(op)
        elif kind == 'file_sent':
            await self._file_sent(op)
        else:
            log.warning('unknown op kind %r; dropping', kind)

    async def _upload_draft(self, op: Any) -> None:
        context = self.context
        store = context.store
        row = store.get_message(op.zoho_folder_id, op.uid)
        if row is None or row.pending_op is None:
            return  # deleted locally meanwhile, or already handled

        def clear_marker() -> None:
            # every terminal path must clear pending_op: an op is dropped
            # once apply() returns, so a stale marker would strand the row
            # "in flight" forever with nothing left to retry it
            store.set_pending_op(op.zoho_folder_id, op.uid, None)

        raw = store.load_raw(op.zoho_folder_id, op.uid)
        if raw is None:
            log.warning('draft uid %d: raw body missing, upload dropped',
                        op.uid)
            clear_marker()
            return
        payload = _draft_payload(raw)
        if payload is None:
            log.warning('draft uid %d: unparseable MIME, upload dropped',
                        op.uid)
            clear_marker()
            return
        # an API error here raises: the op is retried later, and the
        # marker must stay set for the retry to proceed
        zoho_id = await context.client.save_draft(payload)
        if zoho_id:
            store.set_zoho_id(op.zoho_folder_id, op.uid, zoho_id)
        clear_marker()

    async def _file_sent(self, op: Any) -> None:
        """Best-effort filing of a locally-appended sent message.

        If Zoho auto-files sent mail (most likely), the poller's Message-ID
        match in the Sent folder handles linking and this op is a no-op.
        """
        context = self.context
        store = context.store
        row = store.get_message(op.zoho_folder_id, op.uid)
        if row is None:
            return  # deleted locally meanwhile

        def clear_marker() -> None:
            store.set_pending_op(op.zoho_folder_id, op.uid, None)

        if row.zoho_id is not None:
            # linked by the poller while this op was queued
            clear_marker()
            return
        raw = store.load_raw(op.zoho_folder_id, op.uid)
        if raw is None:
            log.warning('sent-copy uid %d: raw body missing, filing dropped',
                        op.uid)
            clear_marker()
            return
        msg_id = _extract_message_id(raw)
        if msg_id:
            for candidate in store.find_by_message_id(msg_id):
                if candidate.zoho_folder_id == op.zoho_folder_id and \
                        candidate.zoho_id:
                    clear_marker()
                    return  # Zoho copy already linked by the poller
        payload = _draft_payload(raw)
        if payload is None:
            log.warning('sent-copy uid %d: unparseable MIME, filing dropped',
                        op.uid)
            clear_marker()
            return
        # an API error here raises; the op retries later with the marker set
        draft_id = await context.client.save_draft(payload)
        if draft_id:
            sent = store.get_folder_by_name('Sent')
            if sent is not None:
                await context.client.move_messages(
                    [draft_id], to_folder_id=sent.zoho_id)
                store.set_zoho_id(op.zoho_folder_id, op.uid, draft_id)
        clear_marker()


def _draft_payload(raw: bytes) -> dict | None:
    """Build the JSON payload for the drafts API from a raw MIME message."""
    try:
        msg: MimeMessage = message_from_bytes(raw)
    except Exception:  # noqa: BLE001
        return None
    to = ', '.join(addr for _, addr in
                   email.utils.getaddresses(msg.get_all('To', [])))
    subject = msg.get('Subject') or ''
    body_text = ''
    body_html = None
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True) or b''
        charset = msg.get_content_charset() or 'utf-8'
        text = payload.decode(charset, 'replace')
        if msg.get_content_type() == 'text/html':
            body_html = text
        else:
            body_text = text
    else:
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            if ctype == 'text/plain' and not body_text:
                payload = part.get_payload(decode=True) or b''
                charset = part.get_content_charset() or 'utf-8'
                body_text = payload.decode(charset, 'replace')
    from_addr = ''
    if msg.get('From'):
        _, from_addr = email.utils.getaddresses([msg['From']])[0]
    return {
        'fromAddress': from_addr,
        'toAddress': to,
        'subject': subject,
        'content': body_html or body_text,
        'mailFormat': 'html' if body_html else 'plaintext',
        'encoding': 'UTF-8',
    }
