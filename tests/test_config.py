"""Settings parsing (env) and validation bounds."""

from __future__ import annotations

import logging

import pytest

from zoho_bridge.config import BridgeSettings

REQUIRED = {
    'ZOHO_CLIENT_ID': 'ci',
    'ZOHO_CLIENT_SECRET': 'cs',
    'ZOHO_REFRESH_TOKEN': 'rt',
    'BRIDGE_IMAP_PASSWORD': 'pw',
}


def _set_required(monkeypatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)


def test_defaults_and_new_env(monkeypatch) -> None:
    monkeypatch.setenv('BRIDGE_HEALTH_HOST', '0.0.0.0')
    monkeypatch.setenv('BRIDGE_TLS_HOSTNAME', 'mail.lan, mail2.lan')
    monkeypatch.setenv('BRIDGE_FULL_SCAN_EVERY', '3')
    settings = BridgeSettings.from_env()
    assert settings.health_host == '0.0.0.0'
    assert settings.tls_hostnames == 'mail.lan, mail2.lan'
    assert settings.full_scan_every == 3
    assert settings.health_port == 8000  # untouched default


def test_bad_int_warns_and_falls_back(monkeypatch, caplog) -> None:
    monkeypatch.setenv('BRIDGE_POLL_SECONDS', '1o0')  # letter-O typo
    with caplog.at_level(logging.WARNING):
        settings = BridgeSettings.from_env()
    assert settings.poll_seconds == 120
    assert any('BRIDGE_POLL_SECONDS' in record.getMessage()
               for record in caplog.records)


def test_validate_requires_credentials(monkeypatch) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ZOHO_CLIENT_ID', 'ci')
    settings = BridgeSettings.from_env()
    with pytest.raises(ValueError, match='ZOHO_REFRESH_TOKEN'):
        settings.validate()


def test_validate_bounds(monkeypatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv('BRIDGE_FULL_SCAN_EVERY', '0')
    with pytest.raises(ValueError, match='FULL_SCAN_EVERY'):
        BridgeSettings.from_env().validate()
    monkeypatch.setenv('BRIDGE_FULL_SCAN_EVERY', '10')
    monkeypatch.setenv('BRIDGE_SMTP_PORT', '99999')
    with pytest.raises(ValueError, match='SMTP_PORT'):
        BridgeSettings.from_env().validate()
    monkeypatch.setenv('BRIDGE_SMTP_PORT', '10587')
    BridgeSettings.from_env().validate()  # all good


def test_dc_bases_and_overrides() -> None:
    settings = BridgeSettings(dc='eu')
    assert settings.accounts_base == 'https://accounts.zoho.eu'
    assert settings.api_base == 'https://mail.zoho.eu/api'
    overridden = BridgeSettings(api_base_override='http://127.0.0.1:9/api/',
                                accounts_base_override='http://127.0.0.1:9/')
    assert overridden.api_base == 'http://127.0.0.1:9/api'
    assert overridden.accounts_base == 'http://127.0.0.1:9'
