"""Data transfer objects for the Zoho Mail REST API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC

__all__ = ['Folder', 'MessageSummary', 'Account']


@dataclass(frozen=True)
class Account:
    account_id: str
    address: str


@dataclass(frozen=True)
class Folder:
    zoho_id: str
    name: str


@dataclass(frozen=True)
class MessageSummary:
    """Metadata of one message as returned by ``messages/view``.

    Field semantics verified against the live API (spike 2026-11-15, see
    ``docs/zoho-api-notes.md``): ``status`` "1" = read / "0" = unread,
    ``flagid`` "important" = starred / "flag_not_set" = unstarred,
    ``receivedTime``/``sentDateInGMT`` = epoch-milliseconds as strings,
    ``messageId`` = numeric string.
    """

    zoho_id: str
    subject: str | None = None
    sender: str | None = None
    received_epoch: float | None = None
    size: int | None = None
    read: bool = False
    starred: bool = False

    @classmethod
    def from_view(cls, msg: dict) -> MessageSummary:
        zoho_id = str(msg.get('messageId') or msg.get('id') or '')
        subject = msg.get('subject')
        sender = msg.get('fromAddress') or msg.get('sender') or \
            msg.get('from')
        received = _first(msg, ('receivedTime', 'sentDateInGMT',
                                'sentDate'))
        size = _first(msg, ('size', 'messageSize', 'mailSize'))
        return cls(
            zoho_id=zoho_id,
            subject=str(subject) if subject is not None else None,
            sender=str(sender) if sender is not None else None,
            received_epoch=_to_epoch(received),
            size=int(size) if isinstance(size, (int, float, str)) and
            str(size).isdigit() else None,
            read=str(_first(msg, ('status', 'isRead', 'read'))) == '1',
            starred=str(_first(msg, ('flagid', 'flagId', 'starred'))) ==
            'important',
        )


def _first(msg: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in msg and msg[key] is not None:
            return msg[key]
    return None


def _to_epoch(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        num = float(val)
        return num / 1000.0 if num > 1e12 else num  # epoch ms vs epoch s
    if isinstance(val, str):
        text = val.strip()
        if text.isdigit():
            num = float(text)
            return num / 1000.0 if num > 1e12 else num
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return None
