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
        # per-folder poll-cycle counters (in-memory by design: a restart
        # resets them so the first pass over every folder is a full sweep)
        self.folder_cycles: dict[str, int] = {}

    def wake_sync(self) -> None:
        if self.sync_wake is not None:
            self.sync_wake.set()

    def next_folder_cycle(self, zoho_id: str) -> int:
        """Return (and increment) the sync-cycle count of a folder."""
        cycle = self.folder_cycles.get(zoho_id, 0)
        self.folder_cycles[zoho_id] = cycle + 1
        return cycle
