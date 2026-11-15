"""Unit tests for ZohoClient request plumbing (httpx.MockTransport).

Covers the early-revocation fix (401/403 forces exactly one token refresh
and retry), the multipart attachment upload (httpx-built body, sanitized
filename), and the raw-MIME decoding heuristic.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from zoho_bridge.config import BridgeSettings
from zoho_bridge.zoho.client import ZohoApiError, ZohoClient, _decode_raw


def _settings() -> BridgeSettings:
    return BridgeSettings(
        client_id='ci', client_secret='cs', refresh_token='rt',
        accounts_base_override='https://accounts.test',
        api_base_override='https://api.test')


def _client(handler) -> ZohoClient:
    return ZohoClient(_settings(),
                      transport=httpx.MockTransport(handler))


ACCOUNT_JSON = {'data': [{'accountId': '42',
                          'primaryEmailAddress': 'me@example.com'}]}


async def test_token_refreshed_once_on_401() -> None:
    state = {'tokens': 0, 'auths': []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/oauth/v2/token':
            state['tokens'] += 1
            return httpx.Response(200, json={
                'access_token': f'T{state["tokens"]}', 'expires_in': 3600})
        state['auths'].append(request.headers.get('Authorization', ''))
        if state['auths'][-1] != 'Zoho-oauthtoken T2':
            return httpx.Response(401, json={'error': 'invalid_token'})
        return httpx.Response(200, json=ACCOUNT_JSON)

    account = await _client(handler).get_account()
    assert account.account_id == '42'
    assert state['tokens'] == 2
    assert state['auths'] == ['Zoho-oauthtoken T1', 'Zoho-oauthtoken T2']


async def test_persistent_401_raises_without_refresh_loop() -> None:
    state = {'tokens': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/oauth/v2/token':
            state['tokens'] += 1
            return httpx.Response(200, json={
                'access_token': f'T{state["tokens"]}', 'expires_in': 3600})
        return httpx.Response(401, json={'error': 'invalid_token'})

    with pytest.raises(ZohoApiError) as excinfo:
        await _client(handler).get_account()
    assert excinfo.value.status == 401
    # initial mint + exactly one forced refresh, no more
    assert state['tokens'] == 2


async def test_upload_attachment_multipart_and_filename_sanitizing() \
        -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/oauth/v2/token':
            return httpx.Response(200, json={
                'access_token': 'T', 'expires_in': 3600})
        if request.url.path.endswith('/accounts'):
            return httpx.Response(200, json=ACCOUNT_JSON)
        captured['ct'] = request.headers.get('Content-Type', '')
        captured['body'] = request.content
        return httpx.Response(200, json={'data': {'attachmentId': 'A9'}})

    aid = await _client(handler).upload_attachment(
        'rd\r\nX-Trick: 1.txt', b'PDFDATA', 'application/pdf')
    assert aid == 'A9'
    ct = captured['ct']
    assert isinstance(ct, str) and ct.startswith('multipart/form-data; '
                                                 'boundary=')
    body = captured['body']
    assert isinstance(body, bytes)
    assert b'PDFDATA' in body
    assert b'name="file"' in body
    # the CRLF from the crafted filename must not survive into headers
    assert b'\r\nX-Trick' not in body


def test_decode_raw_base64_and_text() -> None:
    original = b'\x89PNG' + b'X' * 200
    encoded = base64.b64encode(original).decode()
    assert _decode_raw(encoded) == original
    plain = 'Subject: hi\r\nContent-Type: text/plain\r\n\r\nbody\r\n'
    assert _decode_raw(plain) == plain.encode('utf-8')
    assert _decode_raw(None) == b''
