"""Minimal HTTP health/status endpoint for the bridge (no dependencies)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AbstractAsyncContextManager

from ..context import BridgeContext

__all__ = ['HealthServer']

log = logging.getLogger(__name__)


class HealthServer:
    """Serves ``/healthz`` and ``/status`` on the configured port."""

    def __init__(self, context: BridgeContext) -> None:
        self.context = context

    async def start(self, stack: AbstractAsyncContextManager) -> None:
        settings = self.context.settings
        server = await asyncio.start_server(
            self._handle, host=settings.health_host,
            port=settings.health_port)
        if settings.health_host not in ('127.0.0.1', 'localhost', '::1'):
            log.warning('health/status endpoint exposed on %s — it is '
                        'unauthenticated; consider BRIDGE_HEALTH_HOST='
                        '127.0.0.1', settings.health_host)
        await stack.enter_async_context(server)  # type: ignore[arg-type]
        task = asyncio.create_task(server.serve_forever(), name='health')
        stack.callback(task.cancel)

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), 5.0)
            # drain headers (we route on the request line only)
            while True:
                line = await asyncio.wait_for(reader.readline(), 5.0)
                if line in (b'\r\n', b'\n', b''):
                    break
            path = request_line.split(b' ')[1].decode('ascii', 'replace') \
                if len(request_line.split(b' ')) > 1 else '/'
            if path.startswith('/healthz'):
                ok = self.context.sync_ok
                body = b'ok' if ok else b'starting'
                status = '200 OK' if ok else '503 Service Unavailable'
                await self._respond(writer, status, body,
                                    'text/plain; charset=utf-8')
            elif path.startswith('/status'):
                body = json.dumps(self._status()).encode()
                await self._respond(writer, '200 OK', body,
                                    'application/json')
            else:
                await self._respond(writer, '404 Not Found', b'',
                                    'text/plain')
        except (asyncio.TimeoutError, TimeoutError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    def _status(self) -> dict:
        context = self.context
        store = context.store
        folders = store.list_folders()
        pending_ops, dead_ops = store.op_counts()
        return {
            'sync_ok': context.sync_ok,
            'last_sync_epoch': context.last_sync_epoch,
            'last_sync_age_seconds':
                round(time.time() - context.last_sync_epoch, 1)
                if context.last_sync_epoch else None,
            'folders': {f.imap_name: f.zoho_id for f in folders},
            'pending_ops': pending_ops,
            'dead_ops': dead_ops,
            'raw_cache_bytes': store.raw_cache_size(),
        }

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: str,
                       body: bytes, content_type: str) -> None:
        writer.write(
            f'HTTP/1.0 {status}\r\n'
            f'Content-Type: {content_type}\r\n'
            f'Content-Length: {len(body)}\r\n'
            f'Connection: close\r\n\r\n'.encode() + body)
        await writer.drain()
