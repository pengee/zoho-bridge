"""Unit tests for ZohoClient request plumbing (httpx.MockTransport).

Covers the early-revocation fix (401/403 forces exactly one token refresh
and retry), the multipart attachment upload (httpx-built body, sanitized
filename), the raw-MIME decoding heuristic, and the body-level failure
semantics (Zoho reports failures inside HTTP 200 bodies; mutations must
raise so the ops worker retries).
"""

from __future__ import annotations

import base64

import httpx
import pytest

from zoho_bridge.config import BridgeSettings
from zoho_bridge.zoho.client import (
    ZohoApiError,
    ZohoClient,
    _decode_raw,
    _is_success,
)


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


# -- body-level success semantics -----------------------------------------

def test_is_success_semantics() -> None:
    assert _is_success(200, {'status': {'code': 200,
                                        'description': 'success'}})
    # endpoints without a status node (e.g. the attachment upload) stay
    # trusted on HTTP 2xx
    assert _is_success(200, {'data': {'attachmentId': 'A1'}})
    assert _is_success(204, None)
    assert not _is_success(200, {'status': {'code': 404,
                                            'description': 'Invalid Input'}})
    assert not _is_success(200, {'data': {'errorCode': 'RATE_LIMIT'}})
    assert not _is_success(200, {'status': {'code': 'not-a-number'}})
    assert not _is_success(200, False)
    assert _is_success(200, True)
    assert not _is_success(503, None)
    assert not _is_success(404, {'data': []})


async def test_mutation_raises_on_http200_body_failure() -> None:
    """The exact hazard: Zoho returns 200 with an error body; the ops
    worker must see an exception (retry/backoff), not op_done."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/oauth/v2/token':
            return httpx.Response(200, json={'access_token': 'T',
                                             'expires_in': 3600})
        if request.url.path.endswith('/accounts'):
            return httpx.Response(200, json=ACCOUNT_JSON)
        return httpx.Response(200, json={
            'data': {'errorCode': 'MESSAGE_NOT_FOUND'},
            'status': {'code': 404, 'description': 'Invalid Input'}})

    client = _client(handler)
    with pytest.raises(ZohoApiError) as excinfo:
        await client.mark_read(['123'], read=True)
    assert 'MESSAGE_NOT_FOUND' in str(excinfo.value)
    with pytest.raises(ZohoApiError):
        await client.set_star(['123'], starred=True)
    with pytest.raises(ZohoApiError):
        await client.send(from_addr='me@example.com', to=['x@y.z'],
                          subject='s', text_body='b')


async def test_delete_messages_404_is_idempotent_success() -> None:
    """A retried expunge group must not dead-letter over ids it already
    trashed in a previous partial run (they answer 404 now)."""
    path_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/oauth/v2/token':
            return httpx.Response(200, json={'access_token': 'T',
                                             'expires_in': 3600})
        if request.url.path.endswith('/accounts'):
            return httpx.Response(200, json=ACCOUNT_JSON)
        path_ids.append(request.url.path.rsplit('/', 1)[-1])
        if request.url.path.endswith('/111'):
            return httpx.Response(404, json={
                'status': {'code': 404, 'description': 'not found'}})
        return httpx.Response(200, json={'status': {'code': 200}})

    assert await _client(handler).delete_messages(
        ['111', '222'], '77') is True
    assert path_ids == ['111', '222']


async def test_delete_messages_raises_on_hard_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/oauth/v2/token':
            return httpx.Response(200, json={'access_token': 'T',
                                             'expires_in': 3600})
        if request.url.path.endswith('/accounts'):
            return httpx.Response(200, json=ACCOUNT_JSON)
        return httpx.Response(422, json={'status': {'code': 422}})

    with pytest.raises(ZohoApiError) as excinfo:
        await _client(handler).delete_messages(['5'], '77')
    assert excinfo.value.status == 422


async def test_list_failure_raises_instead_of_empty_view() -> None:
    """A 200+errorCode view must never be mistaken for an emptied folder
    (that would mass-delete messages on a full sweep)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/oauth/v2/token':
            return httpx.Response(200, json={'access_token': 'T',
                                             'expires_in': 3600})
        if request.url.path.endswith('/accounts'):
            return httpx.Response(200, json=ACCOUNT_JSON)
        return httpx.Response(200, json={
            'data': {'errorCode': 'INTERNAL'},
            'status': {'code': 8510087, 'description': 'error'}})

    with pytest.raises(ZohoApiError):
        await _client(handler).list_messages('77')


def test_decode_raw_base64_and_text() -> None:
    original = b'\x89PNG' + b'X' * 200
    encoded = base64.b64encode(original).decode()
    assert _decode_raw(encoded) == original
    plain = 'Subject: hi\r\nContent-Type: text/plain\r\n\r\nbody\r\n'
    assert _decode_raw(plain) == plain.encode('utf-8')
    assert _decode_raw(None) == b''
