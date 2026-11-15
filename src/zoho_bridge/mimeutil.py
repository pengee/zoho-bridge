"""Shared MIME helpers (poller draft ops + SMTP submission)."""

from __future__ import annotations

import email.utils
import logging
from email import message_from_bytes
from email.message import Message as MimeMessage

__all__ = ['extract_message_id', 'addresses', 'split_mime',
           'build_draft_payload']

log = logging.getLogger(__name__)


def extract_message_id(raw: bytes) -> str | None:
    try:
        msg = message_from_bytes(raw)
    except Exception:  # noqa: BLE001 - malformed message
        return None
    return msg.get('Message-ID')


def addresses(msg: MimeMessage, header: str) -> list[str]:
    return [addr for _, addr in
            email.utils.getaddresses(msg.get_all(header, [])) if addr]


def split_mime(msg: MimeMessage) \
        -> tuple[str | None, str | None,
                 list[tuple[str, bytes, str]]]:
    """Extract (text_body, html_body, attachments) from a MIME message."""
    text_body: str | None = None
    html_body: str | None = None
    attachments: list[tuple[str, bytes, str]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        filename = part.get_filename()
        disposition = (part.get('Content-Disposition') or '').lower()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or 'utf-8'
        if filename or 'attachment' in disposition:
            attachments.append((filename or 'attachment.bin', payload, ctype))
        elif ctype == 'text/plain' and text_body is None:
            text_body = payload.decode(charset, 'replace')
        elif ctype == 'text/html' and html_body is None:
            html_body = payload.decode(charset, 'replace')
        elif ctype.startswith('text/') and text_body is None:
            text_body = payload.decode(charset, 'replace')
    if text_body is None and html_body is None and not attachments:
        # degenerate: use the whole payload as text
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            text_body = payload.decode(
                msg.get_content_charset() or 'utf-8', 'replace')
    return text_body, html_body, attachments


def build_draft_payload(raw: bytes) -> dict | None:
    """Build the JSON payload for the drafts API from a raw MIME message.

    Cc is carried over (same body field the send endpoint uses); attachments
    are dropped with a warning — drafting them would need the attachment-id
    upload flow, which the free plan does not permit anyway.
    """
    try:
        msg: MimeMessage = message_from_bytes(raw)
    except Exception:  # noqa: BLE001
        return None
    to = ', '.join(addr for _, addr in
                   email.utils.getaddresses(msg.get_all('To', [])))
    cc = ', '.join(addr for _, addr in
                   email.utils.getaddresses(msg.get_all('Cc', [])))
    subject = msg.get('Subject') or ''
    body_text = ''
    body_html = None
    attachment_count = 0
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
            disposition = (part.get('Content-Disposition') or '').lower()
            if part.get_filename() or 'attachment' in disposition:
                attachment_count += 1
                continue
            ctype = part.get_content_type()
            if ctype == 'text/plain' and not body_text:
                payload = part.get_payload(decode=True) or b''
                charset = part.get_content_charset() or 'utf-8'
                body_text = payload.decode(charset, 'replace')
    if attachment_count:
        log.warning('draft: dropping %d attachment(s) — draft uploads are '
                    'not supported', attachment_count)
    from_addr = ''
    if msg.get('From'):
        _, from_addr = email.utils.getaddresses([msg['From']])[0]
    payload_out = {
        'fromAddress': from_addr,
        'toAddress': to,
        'subject': subject,
        'content': body_html or body_text,
        'mailFormat': 'html' if body_html else 'plaintext',
        'encoding': 'UTF-8',
    }
    if cc:
        payload_out['ccAddress'] = cc
    return payload_out
