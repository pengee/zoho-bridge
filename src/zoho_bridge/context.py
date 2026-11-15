"""Shared services handle for one running bridge instance.

Lives in the core package (not the IMAP layer) so the poller, the SMTP
submission handler, and the health endpoint all depend on this module
instead of on ``imap.backend`` — keeping the dependency graph acyclic.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import BridgeSettings
    from .imap.backend import ZohoMailboxSet
    from .state.store import Store
    from .zoho.client import ZohoClient

__all__ = ['BridgeContext']


class BridgeContext:
    """Shared services for one running bridge instance."""

    def __init__(self, settings: BridgeSettings, client: ZohoClient,
                 store: Store) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.mailbox_set: ZohoMailboxSet | None = None
        # plain asyncio.Event on purpose: pymap's Event leaks one orphaned
        # inner task per wait(timeout=...) call (alpha bug, Python 3.14)
        self.sync_wake: asyncio.Event | None = None
        self.last_sync_epoch: float = 0.0
        self.sync_ok: bool = False

    def wake_sync(self) -> None:
        if self.sync_wake is not None:
            self.sync_wake.set()
