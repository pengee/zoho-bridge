"""End-to-end integration test: the bridge runs as a **real subprocess**
(``python -c "from pymap.main import main; main()" ... zoho``) against the
in-thread fake Zoho API, and the test drives it with real imaplib/smtplib
clients over TCP — the same topology as production.

Covers: login, folder list/select, body fetch, SEARCH, STORE flags (write-
through to Zoho), folder create, UID MOVE, delete+expunge (→ Zoho trash),
SMTP submission with STARTTLS+AUTH, sent-copy dedupe, UID stability.
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
import time
import urllib.request
import uuid

from fake_zoho import FakeZoho, simple_message
from zoho_bridge.imap.backend import ZohoBackend  # noqa: F401 (sanity import)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.2) \
        -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Harness:
    def __init__(self, fake: FakeZoho, tmp_path) -> None:
        self.fake = fake
        self.tmp_path = tmp_path
        self.imap_port = free_port()
        self.smtp_port = free_port()
        self.health_port = free_port()
        self.proc: subprocess.Popen | None = None
        self.log_path = tmp_path / 'bridge.log'

    def start(self, monkeypatch) -> None:
        for key, value in {
            'ZOHO_DC': 'com',
            'ZOHO_API_BASE': f'{self.fake.api}/api',
            'ZOHO_ACCOUNTS_BASE': self.fake.api,
            'ZOHO_CLIENT_ID': 'test-client',
            'ZOHO_CLIENT_SECRET': 'test-secret',
            'ZOHO_REFRESH_TOKEN': 'TEST-REFRESH',
            'BRIDGE_IMAP_USER': 'bridge',
            'BRIDGE_IMAP_PASSWORD': 'bridgepass',
            'BRIDGE_DB_PATH': str(self.tmp_path / 'bridge.db'),
            'BRIDGE_POLL_SECONDS': '1',
            'BRIDGE_SMTP_HOST': '127.0.0.1',
            'BRIDGE_SMTP_PORT': str(self.smtp_port),
            'BRIDGE_HEALTH_PORT': str(self.health_port),
            'BRIDGE_GENERATE_SELF_SIGNED': '1',
        }.items():
            monkeypatch.setenv(key, value)
        log_fh = open(self.log_path, 'wb')  # noqa: SIM115 - closed at stop
        stall_path = self.tmp_path / 'stall.log'
        env = os.environ.copy()
        env['BRIDGE_STALL_LOG'] = str(stall_path)
        boot = os.path.join(os.path.dirname(__file__), 'bridge_boot.py')
        self.proc = subprocess.Popen(
            [sys.executable, boot,
             '--debug', '--host', '127.0.0.1', '--port', str(self.imap_port),
             'zoho'],
            env=env, stdout=log_fh, stderr=subprocess.STDOUT)
        if not wait_until(self._healthy, timeout=45):
            self.dump_log()
            raise AssertionError('bridge did not become healthy — '
                                 'see dumped log above')

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{self.health_port}/status',
                    timeout=3) as resp:
                status = json.loads(resp.read())
            return bool(status.get('sync_ok')) and \
                'INBOX' in status.get('folders', {})
        except Exception:  # noqa: BLE001 - connection refused during startup
            return False

    def dump_log(self) -> None:
        print(f'--- bridge log tail ({self.log_path}) ---')
        if self.log_path.exists():
            lines = self.log_path.read_text(encoding='utf-8',
                                            errors='replace').splitlines()
            for line in lines[-60:]:
                print(line)

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def bridge_log_tail(self, lines: int = 40) -> str:
        if not self.log_path.exists():
            return '<no log>'
        return '\n'.join(self.log_path.read_text(
            encoding='utf-8', errors='replace').splitlines()[-lines:])


def step(msg: str) -> None:
    with open('test_steps.log', 'a', encoding='utf-8') as fh:
        fh.write(f'{time.monotonic():.2f} {msg}\n')


def assert_wait(harness: Harness, predicate, message: str,
                timeout: float = 30.0) -> None:
    if not wait_until(predicate, timeout):
        harness.dump_log()
        raise AssertionError(f'{message}\n--- bridge log tail ---\n'
                             f'{harness.bridge_log_tail()}')


def test_bridge_end_to_end(tmp_path, monkeypatch) -> None:
    fake = FakeZoho()
    m1 = fake.seed_folder(fake.inbox_id, simple_message('Hello one', 'body one'),
                          read=False)
    m2 = fake.seed_folder(fake.inbox_id, simple_message('Hello two', 'body two'),
                          read=True)
    fake.start_in_thread()
    harness = Harness(fake, tmp_path)
    imap = None
    try:
        harness.start(monkeypatch)

        # -- login, list, select ------------------------------------------
        imap = imaplib.IMAP4('127.0.0.1', harness.imap_port)
        assert imap.login('bridge', 'bridgepass')[0] == 'OK'
        typ, listing = imap.list()
        assert typ == 'OK'
        names = b'\n'.join(listing)
        assert b'INBOX' in names and b'Sent' in names and b'Drafts' in names

        typ, data = imap.select('INBOX')
        assert typ == 'OK' and data[0] == b'2', \
            f'INBOX select: {data[0]!r}'

        # -- fetch body ----------------------------------------------------
        typ, fetched = imap.fetch('1', '(BODY.PEEK[])')
        assert typ == 'OK'
        raw = fetched[0][1]
        assert b'Hello one' in raw
        assert b'Message-ID' in raw

        # -- search ----------------------------------------------------------
        typ, res = imap.search(None, 'UNSEEN')
        assert typ == 'OK' and res[0] == b'1'

        # -- flags write-through ----------------------------------------------
        step('STORE1 begin')
        typ, _ = imap.store('1', '+FLAGS', '\\Seen')
        assert typ == 'OK'
        step('STORE1 ok')
        assert_wait(harness, lambda: m1.read,
                    '\\Seen did not reach Zoho')
        step('m1.read=True confirmed')
        typ, _ = imap.store('1', '-FLAGS', '\\Seen')
        assert typ == 'OK'
        step('STORE2 ok')
        assert_wait(harness, lambda: not m1.read,
                    'unread flag did not reach Zoho')
        step('m1.read=False confirmed')

        # -- create folder + uid move -------------------------------------------
        typ, _ = imap.create('Work')
        assert typ == 'OK'
        assert_wait(harness, lambda: 'Work' in fake.folders.values(),
                    'CREATE did not reach Zoho')
        typ, res = imap.uid('SEARCH', None, 'ALL')
        uids = res[0].split()
        assert len(uids) == 2
        typ, _ = imap.uid('MOVE', uids[1].decode(), 'Work')
        assert typ == 'OK'
        work_id = next(fid for fid, name in fake.folders.items()
                       if name == 'Work')
        assert_wait(harness, lambda: m2.folder_id == work_id,
                    'move did not reach Zoho')
        typ, data = imap.select('INBOX')
        assert data[0] == b'1'

        # -- delete + expunge → Zoho trash ---------------------------------------
        typ, res = imap.uid('SEARCH', None, 'ALL')
        remaining = res[0].split()[0].decode()
        typ, _ = imap.uid('STORE', remaining, '+FLAGS', '(\\Deleted)')
        assert typ == 'OK'
        typ, _ = imap.expunge()
        assert typ == 'OK'
        assert_wait(harness, lambda: m1.folder_id == fake.trash_id,
                    'expunge did not reach Zoho trash')

        # -- SMTP submission -------------------------------------------------------
        sent_raw = (
            'From: me@example.com\r\n'
            'To: other@example.com\r\n'
            'Subject: Test send\r\n'
            f'Date: {time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())}\r\n'
            f'Message-ID: <sent-{uuid.uuid4().hex[:8]}@example.com>\r\n'
            'Content-Type: text/plain; charset=utf-8\r\n\r\n'
            'Hello from the test.\r\n'
        ).encode()
        no_verify = ssl.create_default_context()
        no_verify.check_hostname = False
        no_verify.verify_mode = ssl.CERT_NONE
        smtp = smtplib.SMTP('127.0.0.1', harness.smtp_port, timeout=30)
        assert smtp.starttls(context=no_verify)[0] == 220
        smtp.login('bridge', 'bridgepass')
        smtp.sendmail('me@example.com', ['other@example.com'], sent_raw)
        smtp.quit()
        assert_wait(harness,
                    lambda: any(e[0] == 'send' for e in fake.events),
                    'send did not reach fake Zoho')

        # sent copy synced from Zoho's Sent folder (exactly one row)
        assert_wait(harness, lambda: _select_exists(imap, 'Sent') == 1,
                    'sent copy did not sync (or duplicated)')

        # APPEND of the same sent copy must dedupe (subject+time match)
        typ, _ = imap.append('Sent', '(\\Seen)', None, sent_raw)
        assert typ == 'OK'
        exists = _select_exists(imap, 'Sent')
        assert exists == 1, f'sent APPEND was not deduped (EXISTS={exists})'

        # -- star flag write-through on the sent copy -------------------------------
        typ, _ = imap.store('1', '+FLAGS', '\\Flagged')
        assert typ == 'OK'
        sent_msgs = fake.all_messages_in(fake.sent_id)
        assert_wait(harness,
                    lambda: sent_msgs and sent_msgs[0].starred,
                    'star did not reach Zoho')

        # -- uid stability across reconnects ---------------------------------------------
        typ, res = imap.uid('SEARCH', None, 'ALL')
        uids_before = res[0]
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
        imap = None

        def _reconnect() -> bytes:
            client = imaplib.IMAP4('127.0.0.1', harness.imap_port)
            client.login('bridge', 'bridgepass')
            client.select('Sent')
            _, res = client.uid('SEARCH', None, 'ALL')
            uids = res[0]
            client.logout()
            return uids

        assert _reconnect() == uids_before, 'UIDs changed across reconnect'

        # -- health endpoint -----------------------------------------------------------
        with urllib.request.urlopen(
                f'http://127.0.0.1:{harness.health_port}/status',
                timeout=5) as resp:
            status = json.loads(resp.read())
        assert status['sync_ok'] is True
        assert 'INBOX' in status['folders']
        assert status['dead_ops'] == 0, f"dead ops: {status['dead_ops']}"
    finally:
        step(f'finally: m1.read={m1.read} m2.folder={m2.folder_id} '
             f'events={fake.events[-6:]!r}')
        if imap is not None:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass
        harness.stop()
        fake.stop_in_thread()


def _select_exists(imap: imaplib.IMAP4, mailbox: str) -> int:
    typ, data = imap.select(mailbox)
    assert typ == 'OK', f'select {mailbox}: {data}'
    return int(data[0])
