"""In-process fake of the Zoho Mail REST API for integration tests.

Shapes mirror the **live free-plan API** as verified by the phase 0 spike
(see docs/zoho-api-notes.md): message mutations via ``PUT /updatemessage``
with ``mode`` payloads, per-folder DELETE for trashing, ``originalmessage``
with content nested under ``data``, string status/flagid fields in views,
and send auto-filing into the Sent folder.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from aiohttp import web

ACCESS_TOKEN = 'TESTTOKEN'
ACCOUNT_ID = 'acc-test-1'
ADDRESS = 'me@example.com'


def _epoch_ms() -> str:
    return str(int(time.time() * 1000))


def _header(raw: str, name: str) -> str | None:
    for line in raw.splitlines():
        if line.lower().startswith(name.lower() + ':'):
            return line.split(':', 1)[1].strip()
    return None


class FakeMessage:
    def __init__(self, folder_id: str, raw: str, *, read: bool = False,
                 starred: bool = False, message_id: str | None = None):
        # real Zoho messageIds are 19-digit numeric strings
        self.message_id: str = message_id or str(uuid.uuid4().int)[:19]
        self.folder_id = folder_id
        self.raw = raw
        self.read = read
        self.starred = starred
        self.subject = _header(raw, 'Subject') or ''
        self.sender = _header(raw, 'From') or ''
        self.received_ms = _epoch_ms()
        self.size = len(raw)
        self.has_attachment = 'Content-Disposition: attachment' in raw


class FakeZoho:
    """Stateful fake server; run with :meth:`start` in the test's loop."""

    def __init__(self) -> None:
        # real Zoho folder/message ids are long numeric strings
        self.folders: dict[str, str] = {
            str(1794707437000000000 + i): name
            for i, name in enumerate(
                ('Inbox', 'Sent', 'Drafts', 'Trash', 'Spam'))
        }
        self.messages: list[FakeMessage] = []
        self.drafts: list[FakeMessage] = []
        self.attachments: dict[str, tuple[str, bytes]] = {}
        self.events: list[tuple[str, dict]] = []
        self.port = 0
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._thread_loop: asyncio.AbstractEventLoop | None = None

    @property
    def inbox_id(self) -> str:
        return self.folder_id_of('Inbox')

    @property
    def sent_id(self) -> str:
        return self.folder_id_of('Sent')

    @property
    def trash_id(self) -> str:
        return self.folder_id_of('Trash')

    # -- lifecycle ---------------------------------------------------------

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post('/oauth/v2/token', self.handle_token)
        app.router.add_get('/api/accounts', self.handle_accounts)
        app.router.add_get('/api/accounts/{aid}/folders', self.handle_folders)
        app.router.add_post('/api/accounts/{aid}/folders',
                            self.handle_create_folder)
        app.router.add_delete('/api/accounts/{aid}/folders/{fid}',
                              self.handle_delete_folder)
        app.router.add_get('/api/accounts/{aid}/messages/view',
                           self.handle_view)
        app.router.add_get('/api/accounts/{aid}/messages/{mid}/originalmessage',
                           self.handle_original)
        app.router.add_post('/api/accounts/{aid}/messages',
                            self.handle_send_or_draft)
        app.router.add_post('/api/accounts/{aid}/messages/attachments',
                            self.handle_attachment)
        app.router.add_put('/api/accounts/{aid}/updatemessage',
                           self.handle_updatemessage)
        app.router.add_delete('/api/accounts/{aid}/folders/{fid}/'
                              'messages/{mid}',
                              self.handle_delete_message)
        return app

    async def start(self) -> None:
        self.runner = web.AppRunner(self._build_app())
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]  # noqa: SLF001

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    @property
    def api(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    # -- threaded mode (for subprocess-based tests) --------------------------

    def start_in_thread(self) -> int:
        """Run the fake server on a private loop in a daemon thread.

        State is plain dicts — cross-thread reads from the test thread are
        GIL-safe. Returns the port.
        """
        import threading

        ready = threading.Event()
        failed: list[BaseException] = []

        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._thread_loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.start())
            except BaseException as exc:  # noqa: BLE032
                failed.append(exc)
                ready.set()
                return
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.run_until_complete(self.runner.cleanup())
                loop.close()

        thread = threading.Thread(target=_run, daemon=True,
                                  name='fake-zoho')
        thread.start()
        ready.wait(timeout=30)
        if failed:
            raise failed[0]
        return self.port

    def stop_in_thread(self) -> None:
        loop = self._thread_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

    # -- helpers -----------------------------------------------------------

    def auth_ok(self, request: web.Request) -> bool:
        header = request.headers.get('Authorization', '')
        return header == f'Zoho-oauthtoken {ACCESS_TOKEN}'

    def _require_auth(self, request: web.Request) -> web.Response | None:
        if not self.auth_ok(request):
            return web.json_response({'error': 'unauthorized'}, status=401)
        return None

    def find_message(self, mid: str) -> FakeMessage | None:
        for msg in self.messages + self.drafts:
            if msg.message_id == mid:
                return msg
        return None

    def all_messages_in(self, folder_id: str) -> list[FakeMessage]:
        return [m for m in self.messages if m.folder_id == folder_id]

    def seed_folder(self, folder_id: str, raw: str, *, read: bool = False,
                    starred: bool = False) -> FakeMessage:
        msg = FakeMessage(folder_id, raw, read=read, starred=starred)
        self.messages.append(msg)
        return msg

    def folder_id_of(self, zoho_name: str) -> str:
        for fid, name in self.folders.items():
            if name == zoho_name:
                return fid
        raise KeyError(zoho_name)

    def _view_entry(self, m: FakeMessage) -> dict:
        return {
            'messageId': m.message_id,
            'folderId': m.folder_id,
            'subject': m.subject,
            'summary': '',
            'fromAddress': m.sender,
            'sender': m.sender,
            'toAddress': ADDRESS,
            'receivedTime': m.received_ms,
            'sentDateInGMT': m.received_ms,
            'size': m.size,
            'hasAttachment': 'true' if m.has_attachment else 'false',
            'hasInline': 'false',
            'priority': '3',
            'status': '1' if m.read else '0',
            'status2': '0',
            'flagid': 'important' if m.starred else 'flag_not_set',
        }

    # -- endpoints -----------------------------------------------------------

    async def handle_token(self, request: web.Request) -> web.Response:
        form = await request.post()
        if form.get('refresh_token') != 'TEST-REFRESH' or \
                form.get('grant_type') != 'refresh_token':
            return web.json_response({'error': 'invalid_grant'}, status=400)
        return web.json_response({'access_token': ACCESS_TOKEN,
                                  'expires_in': 3600})

    async def handle_accounts(self, request: web.Request) -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        return web.json_response({'data': [{
            'accountId': ACCOUNT_ID,
            'primaryEmailAddress': ADDRESS,
        }]})

    async def handle_folders(self, request: web.Request) -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        return web.json_response({'data': [
            {'folderId': fid, 'folderName': name, 'folderType': 'custom',
             'imapAccess': True, 'isArchived': 0, 'path': '/' + name,
             'folderIcon': 'default', 'VW': True, 'HIDE': False}
            for fid, name in self.folders.items()
        ]})

    async def handle_create_folder(self, request: web.Request) -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        body = await request.json()
        name = body.get('folderName') or body.get('name')
        fid = str(uuid.uuid4().int)[:19]
        self.folders[fid] = name
        self.events.append(('create_folder', {'name': name}))
        return web.json_response({'status': {'code': 200,
                                             'description': 'success'},
                                  'data': {'folderId': fid,
                                           'folderName': name}})

    async def handle_delete_folder(self, request: web.Request) -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        fid = request.match_info['fid']
        self.folders.pop(fid, None)
        self.messages = [m for m in self.messages if m.folder_id != fid]
        return web.json_response({'status': {'code': 200}})

    async def handle_view(self, request: web.Request) -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        folder_id = request.query['folderId']
        limit = int(request.query.get('limit', 50))
        start = int(request.query.get('start', 0))
        msgs = self.all_messages_in(folder_id)
        page = msgs[start:start + limit]
        return web.json_response({'data': [self._view_entry(m)
                                           for m in page]})

    async def handle_original(self, request: web.Request) -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        msg = self.find_message(request.match_info['mid'])
        if msg is None:
            return web.json_response({'error': 'not found'}, status=404)
        return web.json_response({
            'status': {'code': 200, 'description': 'success'},
            'data': {'messageId': int(msg.message_id),
                     'content': msg.raw},
        })

    async def handle_send_or_draft(self, request: web.Request) \
            -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        body = await request.json()
        if body.get('mode') == 'saveDraft':
            raw = (
                f"From: {body.get('fromAddress')}\r\n"
                f"To: {body.get('toAddress')}\r\n"
                f"Subject: {body.get('subject')}\r\n"
                f"Message-ID: <draft-{uuid.uuid4().hex[:10]}@example.com>\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                f"{body.get('content')}\r\n"
            )
            msg = FakeMessage(self.folder_id_of('Drafts'), raw)
            self.drafts.append(msg)
            self.events.append(('draft', {'subject': body.get('subject')}))
            return web.json_response(
                {'status': {'code': 200, 'description': 'success'},
                 'data': {'messageId': msg.message_id}})
        raw = (
            f"From: {body.get('fromAddress')}\r\n"
            f"To: {body.get('toAddress')}\r\n"
            f"Subject: {body.get('subject')}\r\n"
            f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}\r\n"
            f"Message-ID: <fake-{uuid.uuid4().hex[:10]}@example.com>\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body.get('content')}\r\n"
        )
        msg = FakeMessage(self.folder_id_of('Sent'), raw)
        self.messages.append(msg)
        self.events.append(('send', {
            'from': body.get('fromAddress'), 'to': body.get('toAddress'),
            'subject': body.get('subject'),
            'attachments': body.get('attachments'),
        }))
        return web.json_response(
            {'status': {'code': 200, 'description': 'success'},
             'data': {'messageId': msg.message_id,
                      'subject': body.get('subject')}})

    async def handle_attachment(self, request: web.Request) -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        post = await request.post()
        file_field = post.get('file')
        if file_field is None:
            return web.json_response({'error': 'no file'}, status=400)
        aid = 'A-' + uuid.uuid4().hex[:8]
        self.attachments[aid] = (file_field.filename,
                                 file_field.file.read())
        return web.json_response({'data': {'attachmentId': aid,
                                           'name': file_field.filename}})

    async def handle_updatemessage(self, request: web.Request) \
            -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        body = await request.json()
        mode = body.get('mode')
        ids = [str(x) for x in body.get('messageId', [])]
        if mode == 'markAsRead':
            for mid in ids:
                msg = self.find_message(mid)
                if msg is not None:
                    msg.read = True
            self.events.append(('flag', {'messageIds': ids, 'read': True}))
        elif mode == 'markAsUnread':
            for mid in ids:
                msg = self.find_message(mid)
                if msg is not None:
                    msg.read = False
            self.events.append(('flag', {'messageIds': ids, 'read': False}))
        elif mode == 'setFlag':
            starred = body.get('flagid') == 'important'
            for mid in ids:
                msg = self.find_message(mid)
                if msg is not None:
                    msg.starred = starred
            self.events.append(('flag', {'messageIds': ids,
                                         'starred': starred}))
        elif mode == 'moveMessage':
            dest = str(body.get('destfolderId'))
            for mid in ids:
                msg = self.find_message(mid)
                if msg is not None:
                    msg.folder_id = dest
            self.events.append(('move', {'messageIds': ids,
                                         'toFolderId': dest}))
        else:
            return web.json_response(
                {'data': {'errorCode': 'URL_RULE_NOT_CONFIGURED'},
                 'status': {'code': 404, 'description': 'Invalid Input'}},
                status=404)
        return web.json_response({'status': {'code': 200,
                                             'description': 'success'}})

    async def handle_delete_message(self, request: web.Request) \
            -> web.Response:
        denied = self._require_auth(request)
        if denied:
            return denied
        fid = request.match_info['fid']
        mid = request.match_info['mid']
        msg = self.find_message(mid)
        if msg is None or msg.folder_id != fid:
            return web.json_response(
                {'status': {'code': 404, 'description': 'not found'}},
                status=404)
        msg.folder_id = self.folder_id_of('Trash')
        self.events.append(('delete', {'messageIds': [mid]}))
        return web.json_response({'status': {'code': 200,
                                             'description': 'success'},
                                  'cId': 'fake'})


def simple_message(subject: str, body: str, *,
                   message_id: str | None = None) -> str:
    mid = message_id or f'<{uuid.uuid4().hex}@example.com>'
    return (
        f'From: Sender <sender@example.com>\r\n'
        f'To: {ADDRESS}\r\n'
        f'Subject: {subject}\r\n'
        f'Date: Thu, 13 Nov 2026 10:00:00 +0000\r\n'
        f'Message-ID: {mid}\r\n'
        f'Content-Type: text/plain; charset=utf-8\r\n\r\n'
        f'{body}\r\n'
    )
