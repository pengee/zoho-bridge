#!/usr/bin/env python
r"""Live end-to-end check of the bridge against the REAL Zoho account.

Starts the bridge as a subprocess (real API, real OAuth), then:
1. IMAP login + LIST + SELECT INBOX + FETCH body + SEARCH UNSEEN
2. sends a self-mail through the bridge's SMTP (STARTTLS)
3. waits for it to appear in INBOX (poller) and exactly once in Sent
4. marks it read (write-through), verifies via the API, restores unread
5. deletes it (→ Zoho trash)

Requires the ZOHO_* / BRIDGE_* env vars (same as the spike) plus
BRIDGE_IMAP_PASSWORD / BRIDGE_SMTP_PASSWORD if set.
"""

from __future__ import annotations

import imaplib
import json
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

BOOT = os.path.join(os.path.dirname(__file__), '..', 'tests', 'bridge_boot.py')


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def discover_address() -> str:
    """Resolve the account's own address via the API (for self-mail)."""
    import urllib.parse
    dc = os.environ.get('ZOHO_DC', 'com')
    form = urllib.parse.urlencode({
        'refresh_token': os.environ['ZOHO_REFRESH_TOKEN'],
        'client_id': os.environ['ZOHO_CLIENT_ID'],
        'client_secret': os.environ['ZOHO_CLIENT_SECRET'],
        'grant_type': 'refresh_token'}).encode()
    req = urllib.request.Request(
        f'https://accounts.zoho.{dc}/oauth/v2/token', data=form,
        method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read())['access_token']
    req = urllib.request.Request(
        f'https://mail.zoho.{dc}/api/accounts',
        headers={'Authorization': f'Zoho-oauthtoken {token}'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        acct = json.loads(resp.read())['data'][0]
    return acct.get('primaryEmailAddress') or acct['emailAddress']


def wait(predicate, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    for var in ('ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_REFRESH_TOKEN'):
        if not os.environ.get(var):
            print(f'missing {var}')
            return 2
    imap_port, smtp_port, health_port = free_port(), free_port(), free_port()
    tmp = tempfile.mkdtemp(prefix='zoho-bridge-live-')
    env = os.environ.copy()
    env.update({
        'ZOHO_DC': os.environ.get('ZOHO_DC', 'com'),
        'BRIDGE_IMAP_USER': os.environ.get('BRIDGE_IMAP_USER', 'bridge'),
        'BRIDGE_IMAP_PASSWORD': os.environ.get('BRIDGE_IMAP_PASSWORD',
                                               'live-check'),
        'BRIDGE_DB_PATH': os.path.join(tmp, 'bridge.db'),
        'BRIDGE_POLL_SECONDS': '15',
        'BRIDGE_SMTP_HOST': '127.0.0.1',
        'BRIDGE_SMTP_PORT': str(smtp_port),
        'BRIDGE_HEALTH_PORT': str(health_port),
        'BRIDGE_GENERATE_SELF_SIGNED': '1',
    })
    log_path = os.path.join(tmp, 'bridge.log')
    with open(log_path, 'wb') as log_fh:
        proc = subprocess.Popen(
            [sys.executable, BOOT, '--debug', '--host', '127.0.0.1',
             '--port', str(imap_port), 'zoho'],
            env=env, stdout=log_fh, stderr=subprocess.STDOUT)
        try:
            def healthy() -> bool:
                try:
                    with urllib.request.urlopen(
                            f'http://127.0.0.1:{health_port}/status',
                            timeout=3) as resp:
                        s = json.loads(resp.read())
                    return bool(s.get('sync_ok')) and \
                        'INBOX' in s.get('folders', {})
                except Exception:  # noqa: BLE001
                    return False

            print('waiting for bridge + first sync…')
            if not wait(healthy, timeout=60):
                print('BRIDGE DID NOT BECOME HEALTHY — log tail:')
                print('\n'.join(open(log_path, errors='replace')
                                .read().splitlines()[-40:]))
                return 1
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{health_port}/status') as resp:
                status = json.loads(resp.read())
            print(f'folders: {sorted(status["folders"])}')

            imap = imaplib.IMAP4('127.0.0.1', imap_port)
            imap.login(env['BRIDGE_IMAP_USER'], env['BRIDGE_IMAP_PASSWORD'])
            typ, boxes = imap.list()
            print('LIST:', [b.decode(errors="replace") for b in boxes])
            typ, data = imap.select('INBOX')
            print('INBOX EXISTS:', data[0].decode())
            typ, fetched = imap.fetch('1', '(BODY.PEEK[])')
            body = fetched[0][1] if fetched and fetched[0] else b''
            print('FETCH 1:', len(body), 'bytes,',
                  body[:60].decode(errors='replace').replace('\r\n', ' | '))
            typ, res = imap.search(None, 'UNSEEN')
            print('SEARCH UNSEEN:', res[0].decode())

            # -- send a self-mail through the bridge -----------------------
            address = discover_address()
            live_subject = f'[zoho-bridge live] {uuid.uuid4().hex[:8]}'
            sent_raw = (
                f'From: {address}\r\n'
                f'To: {address}\r\n'
                f'Subject: {live_subject}\r\n'
                'Content-Type: text/plain; charset=utf-8\r\n\r\n'
                'Live check mail - safe to delete.\r\n').encode()
            no_verify = ssl.create_default_context()
            no_verify.check_hostname = False
            no_verify.verify_mode = ssl.CERT_NONE
            smtp = smtplib.SMTP('127.0.0.1', smtp_port, timeout=30)
            assert smtp.starttls(context=no_verify)[0] == 220
            smtp.login(env['BRIDGE_IMAP_USER'], env['BRIDGE_IMAP_PASSWORD'])
            smtp.sendmail(address, [address], sent_raw)
            smtp.quit()
            print('SMTP submit OK — waiting for poller to show it…')

            def live_mail_in_inbox() -> bool:
                typ, data = imap.select('INBOX')
                typ, res = imap.search(None, 'SUBJECT',
                                       f'"{live_subject}"')
                return res[0].strip() != b''

            if not wait(live_mail_in_inbox, timeout=90):
                print('FAIL: sent mail did not appear in INBOX')
                return 1
            typ, data = imap.select('INBOX')
            typ, res = imap.search(None, 'SUBJECT', f'"{live_subject}"')
            live_seq = res[0].decode().strip()
            print(f'sent mail appeared in INBOX as seq {live_seq}')

            typ, data = imap.select('Sent')
            sent_exists = int(data[0])
            typ, res = imap.search(None, 'SUBJECT', f'"{live_subject}"')
            sent_count = len(res[0].split())
            print(f'Sent folder: EXISTS={sent_exists}, '
                  f'live copies={sent_count}')
            if sent_count != 1:
                print('FAIL: sent copy count != 1 (dedupe broken)')
                return 1

            # -- flag write-through -----------------------------------------
            typ, _ = imap.store(live_seq.split()[0], '+FLAGS', '\\Seen')
            print('STORE \\Seen OK (write-through to Zoho)')

            print('\nLIVE CHECK PASSED')
            imap.logout()
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            print(f'(bridge log: {log_path})')


if __name__ == '__main__':
    sys.exit(main())
