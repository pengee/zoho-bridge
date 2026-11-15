"""Bridge settings (environment) and the pymap IMAP config implementation."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Final

from pymap.config import BackendCapability, IMAPConfig

__all__ = ['BridgeSettings', 'BridgeConfig']

#: Zoho data center slug -> (accounts base, mail API base)
DC_BASES: Final[dict[str, tuple[str, str]]] = {
    'com': ('https://accounts.zoho.com', 'https://mail.zoho.com'),
    'eu': ('https://accounts.zoho.eu', 'https://mail.zoho.eu'),
    'in': ('https://accounts.zoho.in', 'https://mail.zoho.in'),
    'com.au': ('https://accounts.zoho.com.au', 'https://mail.zoho.com.au'),
    'jp': ('https://accounts.zoho.jp', 'https://mail.zoho.jp'),
    'sa': ('https://accounts.zohosa.com', 'https://mail.zohosa.com'),
    'ca': ('https://accounts.zohocloud.ca', 'https://mail.zohocloud.ca'),
    'cn': ('https://accounts.zoho.com.cn', 'https://mail.zoho.com.cn'),
}


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True)
class BridgeSettings:
    """All environment-driven settings for the bridge."""

    dc: str = 'com'
    client_id: str = ''
    client_secret: str = ''
    refresh_token: str = ''

    imap_user: str = 'bridge'
    imap_password: str = ''
    smtp_user: str = ''
    smtp_password: str = ''

    db_path: str = 'bridge.db'
    poll_seconds: int = 120
    prefetch: int = 25
    raw_cache_mb: int = 512
    smtp_port: int = 10587
    smtp_host: str = '0.0.0.0'
    health_port: int = 8000
    generate_self_signed: bool = True

    account_address: str = field(default='')
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    allow_attachment_loss: bool = False

    #: optional overrides (tests / proxies) — default: derived from ``dc``
    api_base_override: str | None = None
    accounts_base_override: str | None = None

    @classmethod
    def from_env(cls) -> 'BridgeSettings':
        dc = os.environ.get('ZOHO_DC', 'com').strip().lower()
        if dc not in DC_BASES:
            raise ValueError(f'unknown ZOHO_DC {dc!r}; '
                             f'expected one of {sorted(DC_BASES)}')
        imap_password = os.environ.get('BRIDGE_IMAP_PASSWORD', '')
        smtp_user = os.environ.get('BRIDGE_SMTP_USER') or \
            os.environ.get('BRIDGE_IMAP_USER') or 'bridge'
        smtp_password = os.environ.get('BRIDGE_SMTP_PASSWORD') or imap_password
        return cls(
            dc=dc,
            client_id=os.environ.get('ZOHO_CLIENT_ID', ''),
            client_secret=os.environ.get('ZOHO_CLIENT_SECRET', ''),
            refresh_token=os.environ.get('ZOHO_REFRESH_TOKEN', ''),
            imap_user=os.environ.get('BRIDGE_IMAP_USER', 'bridge'),
            imap_password=imap_password,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            db_path=os.environ.get('BRIDGE_DB_PATH', 'bridge.db'),
            poll_seconds=_env_int('BRIDGE_POLL_SECONDS', 120),
            prefetch=_env_int('BRIDGE_PREFETCH', 25),
            raw_cache_mb=_env_int('BRIDGE_RAW_CACHE_MB', 512),
            smtp_port=_env_int('BRIDGE_SMTP_PORT', 10587),
            smtp_host=os.environ.get('BRIDGE_SMTP_HOST', '0.0.0.0'),
            health_port=_env_int('BRIDGE_HEALTH_PORT', 8000),
            generate_self_signed=_env_bool('BRIDGE_GENERATE_SELF_SIGNED',
                                           True),
            allow_attachment_loss=_env_bool('BRIDGE_ALLOW_ATTACHMENT_LOSS',
                                            False),
            account_address=os.environ.get('BRIDGE_ACCOUNT_ADDRESS', ''),
            api_base_override=os.environ.get('ZOHO_API_BASE'),
            accounts_base_override=os.environ.get('ZOHO_ACCOUNTS_BASE'),
        )

    def validate(self) -> None:
        missing = [name for name, val in (
            ('ZOHO_CLIENT_ID', self.client_id),
            ('ZOHO_CLIENT_SECRET', self.client_secret),
            ('ZOHO_REFRESH_TOKEN', self.refresh_token),
            ('BRIDGE_IMAP_PASSWORD', self.imap_password),
        ) if not val]
        if missing:
            raise ValueError(
                f'missing required environment variables: {", ".join(missing)}'
                ' — see docs/zoho-oauth-setup.md')

    @property
    def accounts_base(self) -> str:
        return (self.accounts_base_override or
                DC_BASES[self.dc][0]).rstrip('/')

    @property
    def api_base(self) -> str:
        return (self.api_base_override or
                DC_BASES[self.dc][1] + '/api').rstrip('/')


class BridgeConfig(IMAPConfig):
    """The IMAP config implementation for the ``zoho`` pymap backend."""

    def __init__(self, args: Any, *, bridge: BridgeSettings,
                 **extra: Any) -> None:
        # the default cpu_subsystem runs coroutine chunks on a private
        # per-thread loop (threading Subsystem) — with our asyncio-bound
        # backend that deadlocks; everything here is I/O bound anyway
        from pymap.concurrent import Subsystem
        extra.setdefault('cpu_subsystem', Subsystem.for_asyncio())
        super().__init__(args, admin_key=secrets.token_bytes(32), **extra)
        self.bridge: Final = bridge

    @classmethod
    def parse_args(cls, args: Any) -> dict[str, Any]:
        return {'bridge': BridgeSettings.from_env()}

    @property
    def backend_capability(self) -> BackendCapability:
        return BackendCapability(idle=True, object_id=True, multi_append=True)
