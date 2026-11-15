"""Entry point: delegates to pymap's CLI, which loads the ``zoho`` backend
via the ``pymap.backend`` entry point registered by this package.

Typical invocation::

    pymap --debug --host 0.0.0.0 --port 1143 zoho

Environment (see ``docs/zoho-oauth-setup.md`` and ``config.py``) carries the
Zoho credentials and bridge settings.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

_bg_tasks: set[asyncio.Task] = set()


def _patch_proxy_protocol_task_retention() -> None:
    """Keep strong references to per-connection IMAP tasks.

    ``proxyprotocol.ProxyProtocolReader._read_then_call`` (a dependency of
    pymap) starts the connection handler with ``asyncio.create_task(...)`` and
    discards the reference. The task then survives only via reference cycles
    and is reaped by the cyclic GC seconds later — killing live connections
    with no error. Retain the task until it completes. (Upstream bug; revisit
    when pymap/proxyprotocol releases a fix.)
    """
    from proxyprotocol.reader import ProxyProtocolReader
    from proxyprotocol.result import ProxyResultUnknown
    from proxyprotocol.sock import SocketInfo

    async def _read_then_call_keep(self, callback, timeout, reader, writer):
        try:
            result = await asyncio.wait_for(self.read(reader), timeout)
        except Exception as exc:  # noqa: BLE001 - matches upstream behavior
            writer.close()
            result = ProxyResultUnknown(exc)
        sock_info = SocketInfo.get(writer, result, unique_id=uuid.uuid4().bytes)
        task = asyncio.create_task(callback(reader, writer, sock_info))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)

    ProxyProtocolReader._read_then_call = _read_then_call_keep  # noqa: SLF001


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _patch_proxy_protocol_task_retention()
    from pymap.main import main as pymap_main
    pymap_main()


if __name__ == '__main__':
    main()
