"""SMTP submission shim: local SMTP → Zoho REST API send.

Nextcloud Mail (or any client) submits on port 10587 with STARTTLS + AUTH
PLAIN/LOGIN. The handler parses the MIME message and translates it to the
Zoho send API; attachments are uploaded individually when the API supports
it (see ``docs/zoho-api-notes.md``), otherwise submission fails with a
temporary error rather than silently dropping them.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import ssl
import uuid
from contextlib import AbstractAsyncContextManager
from email import message_from_bytes
from typing import Any

from aiosmtpd.smtp import AuthResult, Envelope, LoginPassword, Session, SMTP

from ..context import BridgeContext
from ..mimeutil import addresses, split_mime

__all__ = ['SubmissionServer']

log = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 20_000_000


class SubmissionHandler:
    """aiosmtpd handler translating DATA → Zoho send."""

    def __init__(self, context: BridgeContext) -> None:
        self.context = context

    # -- auth ------------------------------------------------------------

    def _check_credentials(self, login: bytes, password: bytes) -> bool:
        settings = self.context.settings
        try:
            user = login.decode('utf-8')
            secret = password.decode('utf-8')
        except UnicodeDecodeError:
            return False
        return user == settings.smtp_user and \
            hmac.compare_digest(secret, settings.smtp_password)

    def authenticator(self, server: Any, session: Session,
                      envelope: Envelope, mechanism: str,
                      auth_data: Any) -> AuthResult:
        if isinstance(auth_data, LoginPassword):
            if self._check_credentials(auth_data.login, auth_data.password):
                return AuthResult(success=True)
        return AuthResult(success=False, handled=False)

    # -- envelope ----------------------------------------------------------

    async def handle_RCPT(self, server: SMTP, session: Session,
                          envelope: Envelope, address: str,
                          rcpt_options: list) -> str:
        envelope.rcpt_tos.append(address)
        return '250 OK'

    # -- data ----------------------------------------------------------------

    async def handle_DATA(self, server: SMTP, session: Session,
                          envelope: Envelope) -> str:
        raw: bytes = envelope.original_content or b''
        try:
            await self._submit(raw)
        except _AttachmentUnavailable as exc:
            log.warning('submission rejected (attachments): %s', exc)
            return '451 4.7.0 Attachment upload to Zoho unavailable; try again later'
        except Exception as exc:  # noqa: BLE001 - report as temporary failure
            log.exception('submission failed')
            # collapse newlines: exception text can embed upstream HTTP
            # bodies, which would smuggle forged SMTP responses
            detail = ' '.join(str(exc).split())[:200]
            return f'451 4.3.0 Zoho send failed: {detail}'
        self.context.wake_sync()
        return '250 Message accepted for delivery'

    async def _submit(self, raw: bytes) -> None:
        msg = message_from_bytes(raw)
        settings = self.context.settings
        client = self.context.client

        to = addresses(msg, 'To')
        cc = addresses(msg, 'Cc')
        bcc = addresses(msg, 'Bcc')
        subject = msg.get('Subject') or ''
        from_addr = settings.account_address
        if not from_addr:
            account = await client.get_account()
            from_addr = account.address or envelope.mail_from

        message_id = msg.get('Message-ID')
        if not message_id:
            domain = from_addr.split('@')[-1] or 'localhost'
            message_id = f'<{uuid.uuid4().hex}@{domain}>'
            msg['Message-ID'] = message_id
            raw = msg.as_bytes()

        text_body, html_body, attachments = split_mime(msg)
        attachment_payloads: list[dict] = []
        if attachments and not settings.allow_attachment_loss:
            # verify the upload endpoint works before sending anything,
            # so we never send a mail that silently loses attachments
            for filename, content, content_type in attachments:
                if len(content) > MAX_ATTACHMENT_BYTES:
                    raise _AttachmentUnavailable(
                        f'{filename} exceeds size limit')
                attachment_id = await client.upload_attachment(
                    filename, content, content_type)
                if not attachment_id:
                    raise _AttachmentUnavailable(filename)
                attachment_payloads.append(
                    {'id': attachment_id, 'name': filename})

        await client.send(
            from_addr=from_addr, to=to, cc=cc or None, bcc=bcc or None,
            subject=subject, text_body=text_body, html_body=html_body,
            attachments=attachment_payloads or None)
        log.info('sent message %s (%d attachment(s)) via Zoho API',
                 message_id, len(attachment_payloads))


class _AttachmentUnavailable(Exception):
    pass


class SubmissionServer:
    """Runs the aiosmtpd server inside the bridge's event loop."""

    def __init__(self, context: BridgeContext) -> None:
        self.context = context

    async def start(self, stack: AbstractAsyncContextManager) -> None:
        settings = self.context.settings
        tls_context: ssl.SSLContext | None = None
        if settings.tls_cert_file:
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.load_cert_chain(settings.tls_cert_file,
                                        settings.tls_key_file)
        handler = SubmissionHandler(self.context)

        def _protocol_factory() -> SMTP:
            return SMTP(
                handler,
                ident='zoho-bridge submission',
                tls_context=tls_context,
                require_starttls=False,
                auth_required=True,
                auth_require_tls=True,
                data_size_limit=50_000_000,
                enable_SMTPUTF8=True,
                authenticator=handler.authenticator,
            )

        server = await asyncio.get_running_loop().create_server(
            _protocol_factory, host=settings.smtp_host,
            port=settings.smtp_port)
        await stack.enter_async_context(server)  # type: ignore[arg-type]
        task = asyncio.create_task(server.serve_forever(),
                                   name='smtp-submission')
        stack.callback(task.cancel)
        log.info('SMTP submission listening on %s:%d (STARTTLS %s)',
                 settings.smtp_host, settings.smtp_port,
                 'enabled' if tls_context else 'disabled')
