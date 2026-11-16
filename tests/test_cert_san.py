"""Self-signed TLS cert hygiene: configured SANs on generation and a
startup warning when BRIDGE_TLS_HOSTNAME drifts from the existing cert."""

from __future__ import annotations

import logging
from pathlib import Path

from cryptography import x509

from zoho_bridge.config import BridgeSettings
from zoho_bridge.imap.backend import ZohoBackend

BACKEND_LOGGER = 'zoho_bridge.imap.backend'


def _gen(tmp_path: Path, hostnames: str) -> Path:
    settings = BridgeSettings(tls_hostnames=hostnames)
    cert = tmp_path / 'bridge-cert.pem'
    key = tmp_path / 'bridge-key.pem'
    ZohoBackend._generate_self_signed(str(cert), str(key), settings)
    return cert


def test_generated_cert_carries_configured_sans(tmp_path: Path) -> None:
    cert = _gen(tmp_path, 'mail.lan, localhost')
    loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    ext = loaded.extensions.get_extension_for_class(
        x509.SubjectAlternativeName)
    assert set(ext.value.get_values_for_type(x509.DNSName)) == {
        'mail.lan', 'localhost'}
    # must be a leaf cert, not a CA
    bc = loaded.extensions.get_extension_for_class(
        x509.BasicConstraints).value
    assert not bc.ca


def test_san_drift_warns(tmp_path: Path, caplog) -> None:
    cert = _gen(tmp_path, 'mail.lan')
    with caplog.at_level(logging.WARNING, logger=BACKEND_LOGGER):
        ZohoBackend._warn_on_san_drift(
            str(cert), BridgeSettings(tls_hostnames='bridge.example'))
    assert 'bridge.example' in caplog.text
    assert 'regenerated' in caplog.text


def test_no_warning_when_san_covers_or_unset(tmp_path: Path,
                                             caplog) -> None:
    cert = _gen(tmp_path, 'mail.lan,localhost')
    with caplog.at_level(logging.WARNING, logger=BACKEND_LOGGER):
        ZohoBackend._warn_on_san_drift(
            str(cert), BridgeSettings(tls_hostnames='mail.lan'))
        ZohoBackend._warn_on_san_drift(
            str(cert), BridgeSettings(tls_hostnames=''))
    assert [r.getMessage() for r in caplog.records] == []


def test_unreadable_cert_never_raises(tmp_path: Path) -> None:
    # a garbage file where the cert should be must not break startup
    bogus = tmp_path / 'bridge-cert.pem'
    bogus.write_text('not a cert', encoding='utf-8')
    ZohoBackend._warn_on_san_drift(
        str(bogus), BridgeSettings(tls_hostnames='anything'))
