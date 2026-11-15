"""Bootstrap for running the bridge as a subprocess in tests.

- Windows: pymap installs signal handlers (unsupported on Proactor) — shimmed
  out (production runs on Linux, where this file isn't needed).
- Applies the proxyprotocol task-retention patch (see zoho_bridge/main.py).
- With ``BRIDGE_STALL_LOG`` set, writes periodic asyncio task stacks and
  connection-task lifecycle events there for diagnosing hangs.
"""

import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO)

if os.name == 'nt':
    asyncio.BaseEventLoop.add_signal_handler = lambda *a, **k: None

STALL_LOG = os.environ.get('BRIDGE_STALL_LOG')

_orig_run_forever = asyncio.BaseEventLoop.run_forever


if STALL_LOG:
    import traceback
    import weakref

    _task_creators: 'weakref.WeakKeyDictionary' = weakref.WeakKeyDictionary()

    def _task_factory(loop, coro, **kwargs):
        task = asyncio.Task(coro, **kwargs)
        if 'Event.wait' in repr(coro):
            _task_creators[task] = ''.join(
                traceback.format_stack(limit=6)[:-1])
        return task

    async def _watch() -> None:
        while True:
            await asyncio.sleep(3)
            with open(STALL_LOG, 'a', encoding='utf-8') as fh:
                for t in asyncio.all_tasks():
                    if t.done():
                        continue
                    fh.write(f'{t.get_name()}: {t.get_coro()!r}\n')
                    creator = _task_creators.get(t)
                    if creator:
                        fh.write(creator + '---\n')
                fh.write('===\n')

    def _patched_run_forever(self):
        self.set_task_factory(_task_factory)
        self.create_task(_watch())
        return _orig_run_forever(self)

    asyncio.BaseEventLoop.run_forever = _patched_run_forever

    def _log(msg: str) -> None:
        import time
        with open(STALL_LOG, 'a', encoding='utf-8') as fh:
            fh.write(f'[conn {time.monotonic():.2f}] {msg}\n')

    from pymap.imap import IMAPServer  # noqa: E402

    _orig_call = IMAPServer.__call__

    async def _logged_call(self, *args):
        _log('connection task started')
        try:
            await _orig_call(self, *args)
            _log('connection task returned normally')
        except BaseException as exc:  # noqa: BLE032
            _log('connection task raised: '
                 + ''.join(traceback.format_exception(exc)))
            raise

    IMAPServer.__call__ = _logged_call

from zoho_bridge.main import _patch_proxy_protocol_task_retention  # noqa: E402

_patch_proxy_protocol_task_retention()

from pymap.main import main  # noqa: E402

main()
