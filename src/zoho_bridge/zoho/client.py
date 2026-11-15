"""Thin async client for the Zoho Mail REST API.

Endpoints proven by prior art and the phase 0 spike (see
``docs/zoho-api-notes.md``) are called directly. Endpoints whose exact path /
payload is not yet verified are implemented as small *variant probes*: the
first variant that returns HTTP 200 is remembered for the process lifetime
(and persisted in the store's meta table by the caller).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from typing import Any, Final

import httpx

from ..config import BridgeSettings
from .models import Account, Folder, MessageSummary

__all__ = ['ZohoClient', 'ZohoApiError']

log = logging.getLogger(__name__)

RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class ZohoApiError(Exception):
    """A non-retryable API failure after retries were exhausted."""

    def __init__(self, method: str, url: str, status: int | None,
                 detail: str) -> None:
        super().__init__(f'{method} {url} -> HTTP {status}: {detail}')
        self.status = status


class ZohoClient:
    """One client instance per running bridge (single Zoho account).

    Endpoint shapes verified against a free-plan account (spike 2026-11-15,
    see ``docs/zoho-api-notes.md``). All message mutations go through
    ``PUT /accounts/{aid}/updatemessage`` with a ``mode`` field; messageIds
    are numeric strings in list responses and longs in mutations.
    """

    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()
        self._account: Account | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- auth ------------------------------------------------------------

    async def access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expiry - 60.0:
                return self._token
            form = {
                'refresh_token': self.settings.refresh_token,
                'client_id': self.settings.client_id,
                'client_secret': self.settings.client_secret,
                'grant_type': 'refresh_token',
            }
            url = f'{self.settings.accounts_base}/oauth/v2/token'
            status, data = await self._post_form(url, form)
            if status != 200 or not data.get('access_token'):
                raise ZohoApiError('POST', url, status, json.dumps(data)[:300])
            self._token = data['access_token']
            self._token_expiry = time.monotonic() + \
                float(data.get('expires_in', 3600))
            return self._token

    # -- request plumbing --------------------------------------------------

    async def _post_form(self, url: str, form: dict) -> tuple[int, dict]:
        try:
            resp = await self._http.post(url, data=form)
        except httpx.TransportError as exc:
            raise ZohoApiError('POST', url, None, repr(exc)) from exc
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    async def request(self, method: str, path: str, *,
                      json_body: dict | None = None,
                      params: dict | None = None,
                      content: bytes | None = None,
                      headers: dict | None = None,
                      attempts: int = 4) -> tuple[int, Any]:
        """Authorized JSON request with retry/backoff on transient errors.

        ``path`` is relative to the API base, e.g. ``/accounts``.
        Returns ``(status, parsed json or None)``.
        """
        url = self.settings.api_base + path
        token = await self.access_token()
        req_headers = {'Authorization': f'Zoho-oauthtoken {token}',
                       'Accept': 'application/json'}
        if headers:
            req_headers.update(headers)
        delay = 1.0
        last_error: ZohoApiError | None = None
        for attempt in range(attempts):
            try:
                resp = await self._http.request(
                    method, url, json=json_body, params=params,
                    content=content, headers=req_headers)
            except httpx.TransportError as exc:
                last_error = ZohoApiError(method, url, None, repr(exc))
            else:
                if resp.status_code not in RETRY_STATUSES:
                    try:
                        return resp.status_code, resp.json()
                    except ValueError:
                        return resp.status_code, None
                last_error = ZohoApiError(method, url, resp.status_code,
                                          resp.text[:300])
                retry_after = resp.headers.get('Retry-After')
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            if attempt < attempts - 1:
                await asyncio.sleep(delay + random.uniform(0, 0.5))
                delay *= 2
        raise last_error or ZohoApiError(method, url, None, 'exhausted')

    # -- read endpoints ----------------------------------------------------

    async def get_account(self) -> Account:
        if self._account:
            return self._account
        status, data = await self.request('GET', '/accounts')
        accounts = data.get('data') if isinstance(data, dict) else None
        if status != 200 or not accounts:
            raise ZohoApiError('GET', '/accounts', status, str(data)[:300])
        if isinstance(accounts, list):
            acct = accounts[0]
        else:
            acct = (accounts.get('list') or [None])[0]
        if not acct or not acct.get('accountId'):
            raise ZohoApiError('GET', '/accounts', status, str(data)[:300])
        self._account = Account(
            account_id=str(acct['accountId']),
            address=acct.get('primaryEmailAddress') or
            acct.get('emailAddress') or '')
        return self._account

    async def list_folders(self) -> list[Folder]:
        aid = (await self.get_account()).account_id
        status, data = await self.request('GET', f'/accounts/{aid}/folders')
        if status != 200:
            raise ZohoApiError('GET', '/accounts/{id}/folders', status,
                               str(data)[:300])
        return [Folder(zoho_id=str(f.get('folderId')),
                       name=str(f.get('folderName') or f.get('name') or ''))
                for f in _entries(data) if f.get('folderId')]

    async def list_messages(self, folder_id: str, *, limit: int = 50,
                            start: int = 0) -> list[MessageSummary]:
        aid = (await self.get_account()).account_id
        status, data = await self.request(
            'GET', f'/accounts/{aid}/messages/view',
            params={'folderId': folder_id, 'limit': limit, 'start': start})
        if status != 200:
            raise ZohoApiError('GET', '/messages/view', status,
                               str(data)[:200])
        return [MessageSummary.from_view(m) for m in _entries(data)
                if m.get('messageId')]

    async def fetch_original(self, message_id: str) -> bytes:
        aid = (await self.get_account()).account_id
        status, data = await self.request(
            'GET', f'/accounts/{aid}/messages/{message_id}/originalmessage')
        if status != 200 or not isinstance(data, dict):
            raise ZohoApiError('GET', '/originalmessage', status,
                               str(data)[:200])
        content = data.get('content')
        if content is None and isinstance(data.get('data'), dict):
            content = data['data'].get('content')
        return _decode_raw(content)

    # -- write endpoints -----------------------------------------------------

    async def send(self, *, from_addr: str, to: list[str],
                   cc: list[str] | None = None,
                   bcc: list[str] | None = None, subject: str,
                   text_body: str | None,
                   html_body: str | None = None,
                   attachments: list[dict] | None = None) -> str | None:
        """Send mail; returns Zoho's messageId if the response carries one."""
        aid = (await self.get_account()).account_id
        payload: dict[str, Any] = {
            'fromAddress': from_addr,
            'toAddress': ', '.join(to),
            'subject': subject,
            'mailFormat': 'html' if html_body else 'plaintext',
            'encoding': 'UTF-8',
            'askReceipt': 'no',
        }
        if cc:
            payload['ccAddress'] = ', '.join(cc)
        if bcc:
            payload['bccAddress'] = ', '.join(bcc)
        payload['content'] = html_body or text_body or ''
        if attachments:
            payload['attachments'] = attachments
        status, data = await self.request(
            'POST', f'/accounts/{aid}/messages', json_body=payload)
        if status != 200:
            raise ZohoApiError('POST', '/messages (send)', status,
                               str(data)[:300])
        return _find_id(data)

    async def upload_attachment(self, filename: str, content: bytes,
                               content_type: str) -> str | None:
        """Upload one attachment, returning Zoho's attachment id (or None if
        the endpoint variant does not work)."""
        aid = (await self.get_account()).account_id
        boundary = 'zohobridge-7fa39f'
        body = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'
        ).encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        status, data = await self.request(
            'POST', f'/accounts/{aid}/messages/attachments',
            content=body,
            headers={'Content-Type':
                     f'multipart/form-data; boundary={boundary}'})
        if status != 200:
            log.warning('attachment upload failed (HTTP %s): %s',
                        status, str(data)[:200])
            return None
        return _find_id(data)

    async def save_draft(self, payload: dict) -> str | None:
        """Save a draft via the send endpoint with ``mode: saveDraft``
        (verified; there is no dedicated drafts endpoint)."""
        aid = (await self.get_account()).account_id
        body = {'mode': 'saveDraft', **payload}
        status, data = await self.request(
            'POST', f'/accounts/{aid}/messages', json_body=body)
        if status != 200:
            raise ZohoApiError('POST', '/messages (saveDraft)', status,
                               str(data)[:300])
        return _find_id(data)

    async def mark_read(self, message_ids: list[str], *, read: bool,
                        folder_id: str | None = None) -> bool:
        mode = 'markAsRead' if read else 'markAsUnread'
        aid = (await self.get_account()).account_id
        body = {'mode': mode,
                'messageId': [int(x) for x in message_ids]}
        status, _ = await self.request(
            'PUT', f'/accounts/{aid}/updatemessage', json_body=body)
        return status == 200

    async def set_star(self, message_ids: list[str], *, starred: bool,
                       folder_id: str | None = None) -> bool:
        flag = 'important' if starred else 'flag_not_set'
        aid = (await self.get_account()).account_id
        body = {'mode': 'setFlag', 'flagid': flag,
                'messageId': [int(x) for x in message_ids]}
        status, _ = await self.request(
            'PUT', f'/accounts/{aid}/updatemessage', json_body=body)
        return status == 200

    async def move_messages(self, message_ids: list[str], *,
                            to_folder_id: str,
                            from_folder_id: str | None = None) -> bool:
        aid = (await self.get_account()).account_id
        body: dict[str, Any] = {
            'mode': 'moveMessage',
            'destfolderId': int(to_folder_id),
            'messageId': [int(x) for x in message_ids],
        }
        if from_folder_id:
            body['folderId'] = int(from_folder_id)
            body['isFolderSpecific'] = True
        status, _ = await self.request(
            'PUT', f'/accounts/{aid}/updatemessage', json_body=body)
        return status == 200

    async def delete_messages(self, message_ids: list[str],
                              folder_id: str) -> bool:
        """Delete messages (moves to Zoho Trash — ``expunge=false`` default).

        The documented endpoint is per-message with the source folderId in
        the path.
        """
        aid = (await self.get_account()).account_id
        ok = True
        for mid in message_ids:
            status, _ = await self.request(
                'DELETE', f'/accounts/{aid}/folders/{int(folder_id)}'
                          f'/messages/{int(mid)}')
            ok &= status in (200, 204)
        return ok

    async def create_folder(self, name: str) -> Folder | None:
        aid = (await self.get_account()).account_id
        path = f'/accounts/{aid}/folders'
        for body in ({'folderName': name}, {'name': name}):
            status, data = await self.request('POST', path, json_body=body)
            if status == 200:
                entries = _entries(data)
                if entries and entries[0].get('folderId'):
                    f = entries[0]
                    return Folder(zoho_id=str(f['folderId']),
                                  name=str(f.get('name') or name))
                return Folder(zoho_id='', name=name)
        return None

    async def delete_folder(self, folder_id: str) -> bool:
        aid = (await self.get_account()).account_id
        try:
            status, _ = await self.request(
                'DELETE', f'/accounts/{aid}/folders/{folder_id}')
        except ZohoApiError:
            return False
        return status in (200, 204)


def _entries(data: Any) -> list[dict]:
    """Normalize the known Zoho list shapes: ``{"data": [...]}``,
    ``{"data": {"list": [...]}}``, and single-object ``{"data": {...}}``."""
    if not isinstance(data, dict):
        return []
    inner = data.get('data')
    if isinstance(inner, list):
        return [e for e in inner if isinstance(e, dict)]
    if isinstance(inner, dict):
        lst = inner.get('list')
        if isinstance(lst, list):
            return [e for e in lst if isinstance(e, dict)]
        return [inner]  # single-object response
    return []


def _find_id(data: Any) -> str | None:
    """Dig a messageId-ish value out of an arbitrary success response."""
    if not isinstance(data, dict):
        return None
    inner = data.get('data')
    for node in (data, inner):
        if isinstance(node, dict):
            for key in ('messageId', 'draftId', 'attachmentId', 'id'):
                if node.get(key):
                    return str(node[key])
    return None


def _decode_raw(content: Any) -> bytes:
    """The ``originalmessage`` payload is the raw MIME as text; some DCs may
    base64-encode binary-heavy messages. Detect and decode."""
    if content is None:
        return b''
    if isinstance(content, bytes):
        return content
    text = str(content)
    stripped = ''.join(text.split())
    if len(stripped) > 64 and \
            all(c.isalnum() or c in '+/=' for c in stripped) and \
            '=' not in stripped[:-2]:
        try:
            return base64.b64decode(stripped, validate=True)
        except Exception:  # noqa: BLE001 - fall through to text encoding
            pass
    return text.encode('utf-8', 'surrogateescape')
