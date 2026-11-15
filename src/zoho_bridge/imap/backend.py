"""The ``zoho`` pymap backend.

Serves IMAP4rev1 (+IDLE/UIDPLUS/MOVE/MULTIAPPEND) out of a Zoho Mail account
via the Zoho REST API. In-memory message state mirrors the SQLite store, and
all write operations are written through to the store immediately and queued
as outbox operations for async application on Zoho (see ``poller.py``).

Structure mirrors ``pymap.backend.dict`` — the reference backend plugin.
"""

from __future__ import annotations

import asyncio
import logging
import os
from argparse import ArgumentParser, Namespace
from collections.abc import AsyncIterable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import replace
from datetime import datetime
from typing import Any, Final, TypeVar
from weakref import WeakValueDictionary

from pymap.backend.mailbox import MailboxDataInterface, MailboxSetInterface
from pymap.backend.session import BaseSession
from pymap.concurrent import Event, ReadWriteLock
from pymap.context import subsystem
from pymap.exceptions import (
    AuthorizationFailure,
    InvalidAuth,
    NotAllowedError,
    UserNotFound,
)
from pymap.flags import FlagOp
from pymap.health import HealthStatus
from pymap.interfaces.backend import BackendInterface
from pymap.interfaces.login import IdentityInterface, LoginInterface
from pymap.listtree import ListTree
from pymap.mailbox import MailboxSnapshot
from pymap.message import BaseLoadedMessage, BaseMessage
from pymap.mime import MessageContent
from pymap.parsing.message import AppendMessage
from pymap.parsing.specials import FetchRequirement, ObjectId
from pymap.parsing.specials.flag import Flag, Flagged, Seen
from pymap.selected import SelectedMailbox, SelectedSet
from pymap.token import AllTokens
from pymap.user import UserMetadata
from pysasl.creds.server import ServerCredentials
from pysasl.identity import ClearIdentity

from ..config import BridgeConfig, BridgeSettings
from ..context import BridgeContext
from ..poller import OpsWorker, Poller
from ..smtp.handler import SubmissionServer
from ..state.store import FolderRow, MessageRow, Store, normalize_message_id
from ..zoho.client import ZohoClient
from .health import HealthServer

__all__ = ['ZohoBackend']

log = logging.getLogger(__name__)

MessageT = TypeVar('MessageT', bound='Message')

SYSTEM_FOLDER_NAMES: Final[frozenset[str]] = \
    frozenset({'INBOX', 'Sent', 'Drafts', 'Trash', 'Spam'})


def _flag_strs(flags: Iterable[Flag]) -> frozenset[str]:
    return frozenset(f.value.decode('ascii') for f in flags)


def _flag(flag_str: str) -> Flag:
    return Flag(flag_str)


def _message_id_of(content: MessageContent) -> str | None:
    parsed = content.header.parsed
    for key in (b'message-id', 'message-id', b'Message-ID'):
        try:
            values = parsed[key]
        except (KeyError, TypeError):
            continue
        if values:
            return normalize_message_id(str(values[0]))
    return None


def _subject_of(content: MessageContent) -> str | None:
    parsed = content.header.parsed
    for key in (b'subject', 'subject'):
        try:
            values = parsed[key]
        except (KeyError, TypeError):
            continue
        if values:
            return str(values[0])
    return None


class Message(BaseMessage):
    """A message in the Zoho-backed mailbox store."""

    __slots__ = ['_recent', '_loader', 'zoho_id']

    def __init__(self, uid: int, internal_date: datetime,
                 permanent_flags: Iterable[Flag], *, expunged: bool = False,
                 email_id: ObjectId | None = None,
                 thread_id: ObjectId | None = None,
                 recent: bool = False,
                 zoho_id: str | None = None,
                 loader: ZohoMailboxData | None = None) -> None:
        super().__init__(uid, internal_date, permanent_flags,
                         expunged=expunged,
                         email_id=email_id or
                         (ObjectId(('z' + zoho_id).encode('ascii'))
                          if zoho_id else ObjectId(None)),
                         thread_id=thread_id)
        self._recent = recent
        self._loader = loader
        self.zoho_id = zoho_id

    @classmethod
    def copy(cls, msg: Message, *, uid: int | None = None,
             recent: bool = False, expunged: bool = False) -> Message:
        return cls(uid if uid is not None else msg.uid,
                   msg.internal_date, msg.permanent_flags,
                   expunged=expunged, email_id=msg.email_id,
                   thread_id=msg.thread_id, recent=recent,
                   zoho_id=msg.zoho_id, loader=msg._loader)

    @property
    def recent(self) -> bool:
        return self._recent

    @recent.setter
    def recent(self, recent: bool) -> None:
        self._recent = recent

    async def load_content(self, requirement: FetchRequirement) \
            -> LoadedMessage:
        if self._loader is None:
            raise ValueError('message has no loader')
        content = await self._loader.get_content(self)
        return LoadedMessage(self, requirement, content)


class LoadedMessage(BaseLoadedMessage):
    pass


class _ModSeqTracker:
    """Tracks recent flag updates and expunges, keyed by mod-sequence."""

    def __init__(self) -> None:
        self._highest = 0
        self._updates: dict[int, set[int]] = {}
        self._expunges: dict[int, set[int]] = {}
        self._order: list[int] = []
        self._uids: dict[int, int] = {}

    @property
    def highest(self) -> int:
        return self._highest

    def _record(self, uids: Iterable[int], data: dict[int, set[int]]) -> None:
        self._highest = mod_seq = self._highest + 1
        self._order.append(mod_seq)
        data.setdefault(mod_seq, set()).update(uids)
        for uid in uids:
            prev = self._uids.get(uid)
            self._uids[uid] = mod_seq
            if prev is not None:
                for bucket in (self._updates, self._expunges):
                    bucket.get(prev, set()).discard(uid)
                    if prev in bucket and not bucket[prev]:
                        del bucket[prev]
                        self._order.remove(prev)

    def update(self, uids: Iterable[int]) -> None:
        self._record(uids, self._updates)

    def expunge(self, uids: Iterable[int]) -> None:
        self._record(uids, self._expunges)

    def find_updated(self, mod_seq: int) \
            -> tuple[frozenset[int], frozenset[int]]:
        idx = 0
        for pos, known in enumerate(self._order):
            if known > mod_seq:
                idx = pos
                break
        else:
            return frozenset(), frozenset()
        updates: set[int] = set()
        expunges: set[int] = set()
        for newer in self._order[idx:]:
            updates |= self._updates.get(newer, set())
            expunges |= self._expunges.get(newer, set())
        return frozenset(updates), frozenset(expunges)


class ZohoMailboxData(MailboxDataInterface[Message]):
    """One IMAP mailbox backed by a Zoho folder (mirrored in the store)."""

    def __init__(self, context: BridgeContext, folder: FolderRow) -> None:
        self._context = context
        self._folder = folder
        self._mailbox_id = ObjectId(folder.mailbox_id.encode('ascii'))
        self._updated = subsystem.get().new_event()
        self._messages_lock: ReadWriteLock = subsystem.get().new_rwlock()
        self._selected_set = SelectedSet()
        self._modseq = _ModSeqTracker()
        self._messages: dict[int, Message] = {}
        self._content_cache: WeakValueDictionary[
            tuple[str, int], MessageContent] = WeakValueDictionary()
        self._housekeeping_counter = 0
        self._load_from_store()

    # -- basic properties --------------------------------------------------

    @property
    def folder(self) -> FolderRow:
        return self._folder

    @property
    def mailbox_id(self) -> ObjectId:
        return self._mailbox_id

    @property
    def readonly(self) -> bool:
        return False

    @property
    def uid_validity(self) -> int:
        return self._folder.uid_validity

    @property
    def selected_set(self) -> SelectedSet:
        return self._selected_set

    @property
    def messages_lock(self) -> ReadWriteLock:
        return self._messages_lock

    # -- store loading -------------------------------------------------------

    def _load_from_store(self) -> None:
        self._messages.clear()
        for row in self._context.store.load_messages(self._folder.zoho_id):
            self._messages[row.uid] = self._message_from_row(row, recent=False)
        self._modseq = _ModSeqTracker()
        self._modseq.update(self._messages.keys())

    def _message_from_row(self, row: MessageRow, *, recent: bool) -> Message:
        when = datetime.fromtimestamp(row.internal_date or 0.0)
        return Message(row.uid, when, {_flag(f) for f in row.flags},
                       recent=recent, zoho_id=row.zoho_id, loader=self)

    # -- content loading -------------------------------------------------------

    async def get_content(self, msg: Message) -> MessageContent:
        key = (self._folder.zoho_id, msg.uid)
        content = self._content_cache.get(key)
        if content is not None:
            return content
        raw = await self.load_raw_bytes(msg)
        content = MessageContent.parse(raw)
        self._content_cache[key] = content
        return content

    async def load_raw_bytes(self, msg: Message) -> bytes:
        store = self._context.store
        raw = store.load_raw(self._folder.zoho_id, msg.uid)
        if raw is not None:
            return raw
        if not msg.zoho_id:
            return b''
        client = self._context.client
        raw = await client.fetch_original(msg.zoho_id)
        store.store_raw(self._folder.zoho_id, msg.uid, raw)
        self._prune_cache()
        return raw

    def _prune_cache(self) -> None:
        settings = self._context.settings
        budget = settings.raw_cache_mb * 1024 * 1024
        if self._housekeeping_counter % 25 == 0:
            try:
                self._context.store.prune_raw_cache(budget)
            except Exception:
                log.exception('raw cache prune failed')
        self._housekeeping_counter += 1

    # -- selected-session bookkeeping --------------------------------------

    async def update_selected(self, selected: SelectedMailbox, *,
                              wait_on: Event | None = None) -> SelectedMailbox:
        if wait_on is not None:
            either_event = wait_on.or_event(self._updated)
            await either_event.wait()
        mod_sequence = selected.mod_sequence
        selected.mod_sequence = self._modseq.highest
        if mod_sequence is None:
            all_messages = [self._messages[uid]
                            for uid in sorted(self._messages)]
            selected.add_updates(all_messages, [])
        else:
            updated, expunged = self._modseq.find_updated(mod_sequence)
            updated_messages = [self._messages[uid] for uid in updated
                                if uid in self._messages]
            selected.add_updates(updated_messages, expunged)
        return selected

    async def snapshot(self) -> MailboxSnapshot:
        exists = 0
        recent = 0
        unseen = 0
        first_unseen: int | None = None
        async for msg in self.messages():
            exists += 1
            if msg.recent:
                recent += 1
            if Seen not in msg.permanent_flags:
                unseen += 1
                if first_unseen is None:
                    first_unseen = exists
        return MailboxSnapshot(self.mailbox_id, self.readonly,
                               self.uid_validity, self.permanent_flags,
                               self.session_flags, exists, recent, unseen,
                               first_unseen,
                               max(self._messages, default=0) + 1)

    async def messages(self) -> AsyncIterable[Message]:
        async with self._messages_lock.read_lock():
            for msg in list(self._messages.values()):
                yield msg

    async def claim_recent(self, selected: SelectedMailbox) -> None:
        uids: list[int] = []
        async for msg in self.messages():
            if msg.recent:
                msg.recent = False
                msg_uid = msg.uid
                selected.session_flags.add_recent(msg_uid)
                uids.append(msg_uid)
        if uids:
            self._modseq.update(uids)
            self._updated.set()

    async def cleanup(self) -> None:
        self._prune_cache()

    # -- mutations ------------------------------------------------------------

    async def append(self, append_msg: AppendMessage, *,
                     recent: bool = False) -> Message:
        raw = bytes(append_msg.literal)
        content = MessageContent.parse(raw)
        when = append_msg.when or datetime.now()
        flags = _flag_strs(append_msg.flag_set)
        message_id = _message_id_of(content)
        store = self._context.store
        # sent-copy dedupe: if this Message-ID already exists in the folder,
        # do not create a second copy (Nextcloud Mail APPENDs its sent copy
        # even though the Zoho send already filed one).
        for existing in store.find_by_message_id(message_id):
            if existing.zoho_folder_id == self._folder.zoho_id:
                msg = self._messages.get(existing.uid)
                if msg is not None:
                    return msg
        if self._folder.imap_name == 'Sent':
            # Zoho may rewrite the Message-ID when filing sent mail — fall
            # back to matching a recently-added Zoho copy by subject.
            subject = _subject_of(content)
            match = self._recent_sent_match(subject, message_id,
                                            when.timestamp())
            if match is not None:
                return match
        uid = store.alloc_uids(self._folder.zoho_id)[0]
        store.upsert_message(MessageRow(
            zoho_folder_id=self._folder.zoho_id, uid=uid,
            message_id=message_id, flags=frozenset(flags),
            internal_date=when.timestamp()))
        store.store_raw(self._folder.zoho_id, uid, raw)
        self._queue_upload_ops(uid, message_id, flags)
        async with self._messages_lock.write_lock():
            msg = Message(uid, when, append_msg.flag_set, recent=recent,
                          zoho_id=None, loader=self)
            self._messages[uid] = msg
            self._modseq.update([uid])
            self._updated.set()
        return msg

    def _recent_sent_match(self, subject: str | None,
                           message_id: str | None, when_epoch: float) \
            -> Message | None:
        """Find a Zoho-authored sent copy matching an APPENDed message."""
        store = self._context.store
        for row in store.load_messages(self._folder.zoho_id):
            if not row.zoho_id:
                continue  # only match against copies known to be on Zoho
            id_match = message_id and row.message_id == message_id
            time_match = subject is not None and \
                row.subject == subject and \
                abs(row.internal_date - when_epoch) < 600.0
            if id_match or time_match:
                return self._messages.get(row.uid)
        return None

    def _queue_upload_ops(self, uid: int, message_id: str | None,
                          flags: frozenset[str]) -> None:
        """Queue the Zoho-side write for a locally-appended message."""
        store = self._context.store
        name = self._folder.imap_name
        if name == 'Drafts':
            store.set_pending_op(self._folder.zoho_id, uid, 'upload_draft')
            store.enqueue_op('upload_draft', zoho_folder_id=self._folder.zoho_id,
                             uid=uid, payload={'message_id': message_id})
        elif name == 'Sent':
            # if Zoho auto-files the sent copy (spike-verified), the poller
            # will match it by Message-ID and link it to this row; otherwise
            # the worker attempts a best-effort filing.
            store.enqueue_op('file_sent', zoho_folder_id=self._folder.zoho_id,
                             uid=uid, payload={'message_id': message_id})
            store.set_pending_op(self._folder.zoho_id, uid, 'file_sent')

    async def copy(self, uid: int, destination: ZohoMailboxData, *,
                   recent: bool = False) -> int | None:
        src = self._messages.get(uid)
        if src is None:
            return None
        raw = await self.load_raw_bytes(src)
        return await destination.append_raw(raw, src.internal_date,
                                            src.permanent_flags,
                                            recent=recent)

    async def move(self, uid: int, destination: ZohoMailboxData, *,
                   recent: bool = False) -> int | None:
        src = self._messages.get(uid)
        if src is None:
            return None
        moved_on_zoho = False
        if src.zoho_id and \
                self._folder.zoho_id != destination.folder.zoho_id:
            moved_on_zoho = await self._context.client.move_messages(
                [src.zoho_id], to_folder_id=destination.folder.zoho_id,
                from_folder_id=self._folder.zoho_id)
            if not moved_on_zoho:
                raise NotAllowedError('Zoho move API unavailable — '
                                      'run the spike and check '
                                      'docs/zoho-api-notes.md')
        raw = await self.load_raw_bytes(src)
        dest_uid = await destination.append_raw(
            raw, src.internal_date, src.permanent_flags, recent=recent,
            zoho_id=src.zoho_id if moved_on_zoho else None)
        async with self._messages_lock.write_lock():
            self._messages.pop(uid, None)
            self._modseq.expunge([uid])
            self._updated.set()
        self._context.store.delete_messages(self._folder.zoho_id, [uid])
        return dest_uid

    async def append_raw(self, raw: bytes, when: datetime,
                         flags: frozenset[Flag], *, recent: bool = False,
                         zoho_id: str | None = None) -> int:
        """Insert a fully-materialized message (used by COPY/MOVE)."""
        content = MessageContent.parse(raw)
        message_id = _message_id_of(content)
        store = self._context.store
        uid = store.alloc_uids(self._folder.zoho_id)[0]
        store.upsert_message(MessageRow(
            zoho_folder_id=self._folder.zoho_id, uid=uid, zoho_id=zoho_id,
            message_id=message_id, flags=_flag_strs(flags),
            internal_date=when.timestamp()))
        store.store_raw(self._folder.zoho_id, uid, raw)
        async with self._messages_lock.write_lock():
            msg = Message(uid, when, flags, recent=recent, zoho_id=zoho_id,
                          loader=self)
            self._messages[uid] = msg
            self._modseq.update([uid])
            self._updated.set()
        return uid

    async def get(self, uid: int, cached_msg: Any) -> Message:
        async with self._messages_lock.read_lock():
            msg = self._messages.get(uid)
        if msg is None:
            if not isinstance(cached_msg, Message):
                raise TypeError(cached_msg)
            msg = Message.copy(cached_msg, expunged=True)
        return msg

    async def update(self, uid: int, cached_msg: Any,
                     flag_set: frozenset[Flag], mode: FlagOp) -> Message:
        msg = await self.get(uid, cached_msg)
        old_flags = msg.permanent_flags
        msg.permanent_flags = mode.apply(old_flags, flag_set)
        new_flags = msg.permanent_flags
        store = self._context.store
        store.set_flags(self._folder.zoho_id, uid,
                        _flag_strs(new_flags))
        self._modseq.update([uid])
        self._updated.set()
        if msg.zoho_id:
            self._queue_flag_ops(uid, msg.zoho_id, old_flags, new_flags)
        return msg

    def _queue_flag_ops(self, uid: int, zoho_id: str,
                        old: frozenset[Flag],
                        new: frozenset[Flag]) -> None:
        payload_base = {'zoho_id': zoho_id}
        if Seen in new and Seen not in old:
            self._context.store.enqueue_op(
                'flag_read', zoho_folder_id=self._folder.zoho_id, uid=uid,
                payload={**payload_base, 'read': True})
        elif Seen in old and Seen not in new:
            self._context.store.enqueue_op(
                'flag_read', zoho_folder_id=self._folder.zoho_id, uid=uid,
                payload={**payload_base, 'read': False})
        if Flagged in new and Flagged not in old:
            self._context.store.enqueue_op(
                'flag_star', zoho_folder_id=self._folder.zoho_id, uid=uid,
                payload={**payload_base, 'starred': True})
        elif Flagged in old and Flagged not in new:
            self._context.store.enqueue_op(
                'flag_star', zoho_folder_id=self._folder.zoho_id, uid=uid,
                payload={**payload_base, 'starred': False})

    async def delete(self, uids: Iterable[int]) -> None:
        uids = list(uids)
        zoho_ids: list[str] = []
        async with self._messages_lock.write_lock():
            for uid in uids:
                msg = self._messages.pop(uid, None)
                if msg is not None and msg.zoho_id:
                    zoho_ids.append(msg.zoho_id)
            self._modseq.expunge(uids)
            self._updated.set()
        self._context.store.delete_messages(self._folder.zoho_id, uids)
        if zoho_ids:
            # IMAP delete == move to Zoho Trash (mirrors webmail behavior)
            self._context.store.enqueue_op(
                'expunge',
                payload={'zoho_ids': zoho_ids,
                         'folder_id': self._folder.zoho_id})

    # -- poller entry point ------------------------------------------------------

    async def apply_remote(self, added: list[MessageRow],
                           flag_updates: list[MessageRow],
                           removed_uids: list[int]) -> None:
        """Apply changes discovered by the poller (Zoho → bridge)."""
        async with self._messages_lock.write_lock():
            for row in added:
                if row.uid not in self._messages:
                    self._messages[row.uid] = self._message_from_row(
                        row, recent=True)
            for row in flag_updates:
                msg = self._messages.get(row.uid)
                if msg is not None:
                    msg.permanent_flags = {_flag(f) for f in row.flags}
            self._modseq.update([row.uid for row in added])
            self._modseq.update([row.uid for row in flag_updates])
            self._modseq.expunge(removed_uids)
            for uid in removed_uids:
                self._messages.pop(uid, None)
            self._updated.set()

    def refresh_row(self, row: MessageRow) -> None:
        """Replace a message's row identity (used after sent-copy linking)."""
        self._messages[row.uid] = self._message_from_row(row, recent=False)
        self._updated.set()


class ZohoMailboxSet(MailboxSetInterface[ZohoMailboxData]):
    """The set of mailboxes for the bridge's Zoho account."""

    def __init__(self, context: BridgeContext) -> None:
        super().__init__()
        self._context = context
        self._data: dict[str, ZohoMailboxData] = {}
        self._subscribed: dict[str, bool] = {}
        for folder in context.store.list_folders():
            self._data[folder.imap_name] = \
                ZohoMailboxData(context, folder)
            self._subscribed[folder.imap_name] = True

    @property
    def delimiter(self) -> str:
        return '/'

    def _normalize(self, name: str) -> str:
        if name.upper() == 'INBOX':
            return 'INBOX'
        return name

    def add_data(self, folder: FolderRow) -> ZohoMailboxData:
        data = ZohoMailboxData(self._context, folder)
        self._data[folder.imap_name] = data
        self._subscribed.setdefault(folder.imap_name, True)
        return data

    def evict(self, imap_name: str) -> None:
        self._data.pop(imap_name, None)

    def all_data(self) -> list[ZohoMailboxData]:
        """All live mailbox objects (poller sync targets)."""
        return list(self._data.values())

    async def set_subscribed(self, name: str, subscribed: bool) -> None:
        self._subscribed[self._normalize(name)] = subscribed

    async def list_subscribed(self) -> ListTree:
        names = [name for name, subscribed in self._subscribed.items()
                 if subscribed and name in self._data]
        return ListTree(self.delimiter).update('INBOX', *names)

    async def list_mailboxes(self) -> ListTree:
        return ListTree(self.delimiter).update(*self._data.keys())

    async def get_mailbox(self, name: str) -> ZohoMailboxData:
        name = self._normalize(name)
        if name not in self._data:
            raise KeyError(name)
        return self._data[name]

    async def add_mailbox(self, name: str) -> ObjectId:
        name = self._normalize(name)
        if name in self._data:
            raise ValueError(name)
        if name.upper() == 'INBOX':
            raise ValueError(name)
        folder = await self._context.client.create_folder(name)
        if folder is None or not folder.zoho_id:
            raise NotAllowedError(f'could not create Zoho folder {name!r}')
        row = self._context.store.upsert_folder(
            zoho_id=folder.zoho_id, imap_name=name, zoho_name=folder.name)
        self.add_data(row)
        return ObjectId(row.mailbox_id.encode('ascii'))

    async def delete_mailbox(self, name: str) -> None:
        name = self._normalize(name)
        if name == 'INBOX':
            raise NotAllowedError('cannot delete INBOX')
        if name in SYSTEM_FOLDER_NAMES:
            raise NotAllowedError(f'{name} is a Zoho system folder')
        if name not in self._data:
            raise KeyError(name)
        folder = self._data[name].folder
        if not await self._context.client.delete_folder(folder.zoho_id):
            raise NotAllowedError(f'could not delete Zoho folder {name!r}')
        self._context.store.delete_folder(folder.zoho_id)
        self.evict(name)

    async def rename_mailbox(self, before: str, after: str) -> None:
        raise NotAllowedError('renaming folders is not supported by '
                              'the bridge')


class ZohoSession(BaseSession[Message]):
    """The session implementation for the zoho backend."""

    def __init__(self, owner: str, config: BridgeConfig,
                 mailbox_set: ZohoMailboxSet) -> None:
        super().__init__(owner)
        self._config = config
        self._mailbox_set = mailbox_set

    @property
    def config(self) -> BridgeConfig:
        return self._config

    @property
    def mailbox_set(self) -> ZohoMailboxSet:
        return self._mailbox_set


class ZohoLogin(LoginInterface):
    """Static-credential login; all users see the one bridged Zoho account."""

    def __init__(self, config: BridgeConfig,
                 mailbox_set: ZohoMailboxSet | None = None) -> None:
        super().__init__()
        self.config: Final = config
        self.mailbox_set = mailbox_set
        self._tokens = AllTokens(config)

    @property
    def tokens(self) -> AllTokens:
        return self._tokens

    def set_mailbox_set(self, mailbox_set: ZohoMailboxSet) -> None:
        self.mailbox_set = mailbox_set

    async def authenticate(self, credentials: ServerCredentials) \
            -> Identity:
        settings = self.config.bridge
        expected = ClearIdentity(settings.imap_user, settings.imap_password)
        if not credentials.verify(expected):
            await asyncio.sleep(self.config.invalid_user_sleep)
            raise InvalidAuth()
        return Identity(credentials.authcid, self)

    async def authorize(self, authenticated: IdentityInterface,
                        authzid: str) -> Identity:
        if authenticated.name != authzid:
            raise AuthorizationFailure()
        return Identity(authzid, self)


class Identity(IdentityInterface):
    """The identity implementation for the zoho backend."""

    def __init__(self, name: str, login: ZohoLogin) -> None:
        super().__init__()
        self.login: Final = login
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def roles(self) -> frozenset[str]:
        return frozenset()

    async def new_token(self, *, expiration: datetime | None = None) \
            -> str | None:
        return None

    @asynccontextmanager
    async def new_session(self):
        if self.login.mailbox_set is None:
            raise UserNotFound(self.name)
        yield ZohoSession(self.name, self.login.config,
                          self.login.mailbox_set)

    async def get(self) -> UserMetadata:
        return UserMetadata(self.login.config, self.name)

    async def set(self, metadata: UserMetadata) -> int | None:
        raise NotAllowedError('bridge users are static')

    async def delete(self) -> None:
        raise NotAllowedError('bridge users are static')


class ZohoBackend(BackendInterface):
    """Defines the backend that bridges IMAP to a Zoho Mail account."""

    def __init__(self, login: ZohoLogin, config: BridgeConfig) -> None:
        super().__init__()
        self._login = login
        self._config = config
        self._status = HealthStatus()
        self.context: BridgeContext | None = None

    @property
    def login(self) -> ZohoLogin:
        return self._login

    @property
    def config(self) -> BridgeConfig:
        return self._config

    @property
    def status(self) -> HealthStatus:
        return self._status

    @classmethod
    def add_subparser(cls, name: str, subparsers: Any) -> ArgumentParser:
        parser: ArgumentParser = subparsers.add_parser(
            name, help='Zoho Mail REST API bridge')
        parser.add_argument('--require-tls-cert', action='store_true',
                            help='fail startup if no TLS cert is configured')
        return parser

    @classmethod
    async def init(cls, args: Namespace, **overrides: Any) \
            -> tuple[ZohoBackend, BridgeConfig]:
        settings = BridgeSettings.from_env()
        settings.validate()
        cert_file, key_file = cls._ensure_certificates(args, settings)
        settings = replace(settings, tls_cert_file=cert_file,
                           tls_key_file=key_file)
        config = BridgeConfig(
            args,
            host=getattr(args, 'host', None),
            port=getattr(args, 'port', 143),
            debug=getattr(args, 'debug', False),
            cert_file=cert_file,
            key_file=key_file,
            tls_enabled=getattr(args, 'tls', True),
            bridge=settings,
            **overrides)
        login = ZohoLogin(config)
        return cls(login, config), config

    @classmethod
    def _ensure_certificates(cls, args: Namespace,
                             settings: BridgeSettings) \
            -> tuple[str | None, str | None]:
        cert_file = getattr(args, 'cert', None) or os.environ.get('CERT_FILE')
        key_file = getattr(args, 'key', None) or os.environ.get('KEY_FILE')
        if cert_file and key_file:
            return cert_file, key_file
        if getattr(args, 'require_tls_cert', False):
            raise ValueError(
                '--require-tls-cert is set but no TLS certificate is '
                'configured — provide CERT_FILE/KEY_FILE (or --cert/--key)')
        if not settings.generate_self_signed:
            return None, None
        cert_dir = os.path.dirname(os.path.abspath(settings.db_path)) or '.'
        cert_path = os.path.join(cert_dir, 'bridge-cert.pem')
        key_path = os.path.join(cert_dir, 'bridge-key.pem')
        if not (os.path.exists(cert_path) and os.path.exists(key_path)):
            cls._generate_self_signed(cert_path, key_path, settings)
            log.info('generated self-signed TLS certificate at %s', cert_path)
        return cert_path, key_path

    @classmethod
    def _generate_self_signed(cls, cert_path: str, key_path: str,
                              settings: BridgeSettings) -> None:
        import socket
        from datetime import timedelta

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        hostnames = [n.strip() for n in
                     settings.tls_hostnames.split(',') if n.strip()]
        if not hostnames:
            hostnames = [socket.gethostname(), 'localhost']
        key = rsa.generate_private_key(public_exponent=65537,
                                       key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                settings.imap_user)])
        now = datetime.now()
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=825))
            # leaf certificate: CA must be False; verification relies on the
            # SAN (CN is ignored by modern TLS stacks)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName(n) for n in hostnames]),
                critical=False)
            .sign(key, hashes.SHA256())
        )
        # create the key with owner-only permissions from the start
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as fh:
            fh.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        with open(cert_path, 'wb') as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))

    async def start(self, stack: AsyncExitStack) -> None:
        settings = self._config.bridge
        store = Store(settings.db_path)
        client = ZohoClient(settings)
        context = BridgeContext(settings, client, store)
        self.context = context
        context.sync_wake = asyncio.Event()

        mailbox_set = ZohoMailboxSet(context)
        context.mailbox_set = mailbox_set
        self._login.set_mailbox_set(mailbox_set)

        # background workers
        poller = Poller(context)
        ops = OpsWorker(context)
        smtp = SubmissionServer(context)
        health = HealthServer(context)

        stack.push_async_callback(self._stop)
        await smtp.start(stack)
        await health.start(stack)
        for task_factory in (poller.run_forever, ops.run_forever):
            task = asyncio.create_task(task_factory(),
                                       name=task_factory.__qualname__)
            stack.callback(task.cancel)
        log.info('zoho-bridge started: IMAP via pymap, SMTP on %s:%d, '
                 'health on :%d, poll every %ds',
                 settings.smtp_host, settings.smtp_port,
                 settings.health_port, settings.poll_seconds)

    async def _stop(self) -> None:
        context = self.context
        if context is None:
            return
        with suppress(Exception):  # shutdown must not fail
            await context.client.aclose()
        context.store.close()
