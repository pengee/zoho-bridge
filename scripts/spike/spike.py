#!/usr/bin/env python
r"""Phase 0 feasibility spike for zoho-bridge.

Probes the Zoho Mail REST API endpoints the bridge needs, **on your real
account** (stdlib only, no dependencies). Endpoint shapes follow the
documented API (https://www.zoho.com/mail/help/api) — verified against the
free plan on 2026-11-14/15 (see docs/zoho-api-notes.md). Safe by
construction: flag probes are reverted, move/delete probes target the test
mail this script sends to itself, and sending mails *yourself*.

Usage:

    ZOHO_DC=com
    ZOHO_CLIENT_ID=1000.XXXXXXXXXXXX
    ZOHO_CLIENT_SECRET=xxxxxxxxxxxxxxxx
    ZOHO_REFRESH_TOKEN=1000.xxx.xxx        (or omit to paste a grant code)
    python scripts/spike/spike.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

DC_ACCOUNTS = {
    "com": "https://accounts.zoho.com",
    "eu": "https://accounts.zoho.eu",
    "in": "https://accounts.zoho.in",
    "com.au": "https://accounts.zoho.com.au",
    "jp": "https://accounts.zoho.jp",
    "sa": "https://accounts.zohosa.com",
    "ca": "https://accounts.zohocloud.ca",
    "cn": "https://accounts.zoho.com.cn",
}
DC_MAIL = {
    "com": "https://mail.zoho.com",
    "eu": "https://mail.zoho.eu",
    "in": "https://mail.zoho.in",
    "com.au": "https://mail.zoho.com.au",
    "jp": "https://mail.zoho.jp",
    "sa": "https://mail.zohosa.com",
    "ca": "https://mail.zohocloud.ca",
    "cn": "https://mail.zoho.com.cn",
}

NOTES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "docs",
                          "zoho-api-notes.md")
TIMEOUT = 30


def http(method: str, url: str, *, token: str | None = None,
         form: dict | None = None, json_body: dict | None = None,
         raw_body: bytes | None = None, content_type: str | None = None):
    headers = {"User-Agent": "zoho-bridge-spike/0.2"}
    if token:
        headers["Authorization"] = f"Zoho-oauthtoken {token}"
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        if content_type:
            headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001 - report everything in the log
        return None, repr(exc).encode()


def jparse(body):
    if isinstance(body, bytes):
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return None
    return body


def excerpt(body, limit: int = 400) -> str:
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    else:
        text = str(body)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


class Probe:
    def __init__(self, name: str, critical: bool = False):
        self.name = name
        self.critical = critical
        self.verdict = "SKIP"
        self.detail = ""

    def ok(self, detail: str = "") -> None:
        self.verdict, self.detail = "PASS", detail

    def fail(self, detail: str = "") -> None:
        self.verdict, self.detail = "FAIL", detail

    def inconclusive(self, detail: str = "") -> None:
        self.verdict, self.detail = "INCONCLUSIVE", detail


def entries_of(data) -> list[dict]:
    """Normalize known Zoho list shapes."""
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    if isinstance(inner, list):
        return [e for e in inner if isinstance(e, dict)]
    if isinstance(inner, dict):
        lst = inner.get("list")
        if isinstance(lst, list):
            return [e for e in lst if isinstance(e, dict)]
        return [inner]
    return []


def dig(data, key: str):
    """Find a key at the top level or inside a nested `data` object."""
    if isinstance(data, dict):
        if data.get(key) is not None:
            return data[key]
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get(key) is not None:
            return inner[key]
    return None


class Spike:
    def __init__(self, dc: str, client_id: str, client_secret: str,
                 refresh_token: str | None):
        self.dc = dc
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.account_id: str | None = None
        self.address: str | None = None
        self.probes: list[Probe] = []
        self.api = DC_MAIL.get(dc, DC_MAIL["com"]).rstrip("/") + "/api"
        self.accounts_base = DC_ACCOUNTS.get(dc, DC_ACCOUNTS["com"]).rstrip("/")

    # -- auth ---------------------------------------------------------

    def p_refresh_or_exchange(self) -> Probe:
        p = Probe("OAuth: get access token", critical=True)
        if self.refresh_token:
            status, body = http(
                "POST", f"{self.accounts_base}/oauth/v2/token",
                form={"refresh_token": self.refresh_token,
                      "client_id": self.client_id,
                      "client_secret": self.client_secret,
                      "grant_type": "refresh_token"})
            data = jparse(body)
            if status == 200 and data and data.get("access_token"):
                self.access_token = data["access_token"]
                p.ok("refresh_token grant ok")
            else:
                p.fail(f"HTTP {status}: {excerpt(body)}")
            return p
        print("\nNo ZOHO_REFRESH_TOKEN set — paste the one-time grant code "
              "from the API console:")
        code = input("grant code> ").strip()
        status, body = http(
            "POST", f"{self.accounts_base}/oauth/v2/token",
            form={"code": code, "client_id": self.client_id,
                  "client_secret": self.client_secret,
                  "grant_type": "authorization_code"})
        data = jparse(body)
        if status == 200 and data and data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]
            self.access_token = data.get("access_token")
            print(f"\n  *** SAVE THIS REFRESH TOKEN (never expires unless "
                  f"revoked):\n  *** {self.refresh_token}\n")
            if not self.access_token:
                st2, b2 = http(
                    "POST", f"{self.accounts_base}/oauth/v2/token",
                    form={"refresh_token": self.refresh_token,
                          "client_id": self.client_id,
                          "client_secret": self.client_secret,
                          "grant_type": "refresh_token"})
                d2 = jparse(b2)
                if st2 == 200 and d2 and d2.get("access_token"):
                    self.access_token = d2["access_token"]
            p.ok("authorization_code grant ok, refresh token minted")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)}")
        return p

    # -- read path ------------------------------------------------------

    def p_accounts(self) -> Probe:
        p = Probe("GET /api/accounts (list accounts)", critical=True)
        status, body = http("GET", f"{self.api}/accounts",
                            token=self.access_token)
        accounts = entries_of(jparse(body))
        if status == 200 and accounts:
            acct = accounts[0]
            self.account_id = str(acct.get("accountId", ""))
            self.address = acct.get("primaryEmailAddress") or \
                acct.get("emailAddress") or ""
            p.ok(f"account id={self.account_id} address={self.address}")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)}")
        return p

    def p_folders(self) -> Probe:
        p = Probe("GET /api/accounts/{id}/folders", critical=True)
        status, body = http(
            "GET", f"{self.api}/accounts/{self.account_id}/folders",
            token=self.access_token)
        folders = entries_of(jparse(body))
        if status == 200 and folders:
            first = folders[0]
            self.folder_ids = {}
            for f in folders:
                name = f.get("folderName") or f.get("name") or ""
                self.folder_ids[str(name)] = str(f.get("folderId"))
            p.ok(f"{len(folders)} folders; fields={sorted(first.keys())}; "
                 f"first={excerpt(json.dumps(first), 220)}")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)}")
        return p

    def _inbox_id(self) -> str | None:
        for name in ("Inbox", "INBOX", "inbox"):
            if name in self.folder_ids:
                return self.folder_ids[name]
        return None

    def p_view(self) -> Probe:
        p = Probe("GET /messages/view (free plan read)", critical=True)
        fid = self._inbox_id()
        if not fid:
            p.inconclusive("no Inbox folder id known")
            return p
        status, body = http(
            "GET", f"{self.api}/accounts/{self.account_id}/messages/view"
                   f"?folderId={fid}&limit=5", token=self.access_token)
        msgs = entries_of(jparse(body))
        if status == 200 and msgs:
            first = msgs[0]
            self.first_message_id = str(first.get("messageId") or "")
            self.view_folder_ids = {str(m.get("messageId")):
                                    str(m.get("folderId")) for m in msgs}
            p.ok(f"{len(msgs)} messages; fields={sorted(first.keys())}; "
                 f"first={excerpt(json.dumps(first), 300)}")
        elif status == 200:
            p.ok("0 messages (empty Inbox) — flag probes will use sent mail")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)}")
        return p

    def p_pagination(self) -> Probe:
        p = Probe("messages/view pagination (start param)")
        fid = self._inbox_id()
        if not fid:
            p.inconclusive("no Inbox folder id known")
            return p
        st1, b1 = http("GET", f"{self.api}/accounts/{self.account_id}"
                              f"/messages/view?folderId={fid}&limit=100",
                       token=self.access_token)
        st2, b2 = http("GET", f"{self.api}/accounts/{self.account_id}"
                              f"/messages/view?folderId={fid}&limit=5&start=5",
                       token=self.access_token)
        ids1 = [str(m.get("messageId"))
                for m in entries_of(jparse(b1))[:5]]
        ids2 = [str(m.get("messageId"))
                for m in entries_of(jparse(b2))[:5]]
        if st1 == 200 and st2 == 200:
            p.ok(f"start=5 returned "
                 f"{'DIFFERENT (offset works)' if ids2 and ids2 != ids1 else 'SAME as offset 0 (start ignored?)'}")
        else:
            p.fail(f"HTTP {st1}/{st2}")
        return p

    def p_original(self) -> Probe:
        p = Probe("GET /messages/{id}/originalmessage (raw MIME)",
                  critical=True)
        mid = getattr(self, "first_message_id", "") or \
            getattr(self, "sent_message_id", "")
        if not mid:
            p.inconclusive("no message id known")
            return p
        status, body = http(
            "GET", f"{self.api}/accounts/{self.account_id}/messages/"
                   f"{mid}/originalmessage", token=self.access_token)
        content = dig(jparse(body), "content")
        if status == 200 and content:
            p.ok(f"raw MIME {len(str(content))} chars "
                 f"(nested under data), starts: {str(content)[:60]!r}")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)}")
        return p

    # -- mutations ---------------------------------------------------------

    def _updatemessage(self, payload: dict):
        return http("PUT",
                    f"{self.api}/accounts/{self.account_id}/updatemessage",
                    token=self.access_token, json_body=payload)

    def p_flags(self) -> Probe:
        p = Probe("PUT /updatemessage markAsRead / markAsUnread")
        mid = getattr(self, "first_message_id", "") or \
            getattr(self, "sent_message_id", "")
        if not mid:
            p.inconclusive("no message id known")
            return p
        st1, b1 = self._updatemessage(
            {"mode": "markAsRead", "messageId": [int(mid)]})
        st2, b2 = self._updatemessage(
            {"mode": "markAsUnread", "messageId": [int(mid)]})
        if st1 == 200 and st2 == 200:
            p.ok(f"markAsRead HTTP {st1}, markAsUnread HTTP {st2} "
                 f"(message left unread)")
        else:
            p.fail(f"read: HTTP {st1} {excerpt(b1, 120)}; "
                   f"unread: HTTP {st2} {excerpt(b2, 120)}")
        return p

    def p_star(self) -> Probe:
        p = Probe("PUT /updatemessage flag (star = 'important')")
        mid = getattr(self, "first_message_id", "") or \
            getattr(self, "sent_message_id", "")
        if not mid:
            p.inconclusive("no message id known")
            return p
        tried = []
        for mode, field in (("setFlag", "flagid"), ("flag", "flagid"),
                            ("setFlag", "name"), ("flagEmail", "flagid")):
            st, b = self._updatemessage(
                {"mode": mode, field: "important", "messageId": [int(mid)]})
            tried.append(f"{mode}/{field}: HTTP {st} {excerpt(b, 60)}")
            if st == 200:
                # unset again
                self._updatemessage(
                    {"mode": mode, field: "flag_not_set",
                     "messageId": [int(mid)]})
                p.ok("; ".join(tried))
                return p
        p.fail("no flag variant returned 200; tried " + " | ".join(tried))
        return p

    def p_send(self) -> Probe:
        p = Probe("POST /messages (send)", critical=True)
        if not self.address:
            p.inconclusive("no account address known")
            return p
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        subject = f"[zoho-bridge spike] plain send {stamp}"
        payload = {"fromAddress": self.address,
                   "toAddress": self.address,
                   "subject": subject,
                   "content": "Phase 0 spike of zoho-bridge. Safe to delete.",
                   "mailFormat": "plaintext",
                   "encoding": "UTF-8",
                   "askReceipt": "no"}
        status, body = http("POST",
                            f"{self.api}/accounts/{self.account_id}/messages",
                            token=self.access_token, json_body=payload)
        new_id = dig(jparse(body), "messageId")
        if status == 200 and new_id:
            self.sent_message_id = str(new_id)
            p.ok(f"sent; messageId={self.sent_message_id}")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)}")
        return p

    def p_sent_filing(self) -> Probe:
        p = Probe("does send auto-file into the Sent folder?")
        if not getattr(self, "sent_message_id", ""):
            p.inconclusive("send probe did not run")
            return p
        time.sleep(3)
        sent_fid = None
        for name in ("Sent", "Sent Mail", "sent"):
            if name in self.folder_ids:
                sent_fid = self.folder_ids[name]
                break
        if not sent_fid:
            p.inconclusive(f"no Sent folder in: {list(self.folder_ids)}")
            return p
        status, body = http(
            "GET", f"{self.api}/accounts/{self.account_id}/messages/view"
                   f"?folderId={sent_fid}&limit=10", token=self.access_token)
        msgs = entries_of(jparse(body))
        match = any(str(m.get("messageId")) == self.sent_message_id
                    for m in msgs)
        if match:
            p.ok("sent copy present in Sent (match by messageId)")
        else:
            p.fail("not found in Sent after send")
        return p

    def p_attachment_upload(self) -> Probe:
        p = Probe("attachment upload (POST /messages/attachments)")
        boundary = "zohobridgespike"
        payload = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; "
            f"filename=\"spike.txt\"\r\n"
            f"Content-Type: text/plain\r\n\r\n"
            f"spike attachment\r\n"
            f"--{boundary}--\r\n").encode()
        status, body = http(
            "POST", f"{self.api}/accounts/{self.account_id}/messages/"
                    f"attachments",
            token=self.access_token, raw_body=payload,
            content_type=f"multipart/form-data; boundary={boundary}")
        if status == 200 and dig(jparse(body), "attachmentId"):
            p.ok(f"uploaded: {excerpt(body, 150)}")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)} (bridge degrades: "
                   f"rejects submissions with attachments)")
        return p

    def p_drafts(self) -> Probe:
        p = Probe("save draft (POST /messages with draft mode?)")
        if not self.address:
            p.inconclusive("no account address known")
            return p
        stamp = datetime.now(UTC).strftime("%H%M%S")
        base = {"fromAddress": self.address,
                "toAddress": self.address,
                "subject": f"[zoho-bridge spike] draft {stamp}",
                "content": "draft created by spike - will be deleted",
                "mailFormat": "plaintext",
                "encoding": "UTF-8"}
        tried = []
        for extra in ({"mode": "saveDraft"}, {"action": "saveDraft"},
                      {"type": "draft"}, {"saveAs": "draft"},
                      {"prop": "draft"}):
            payload = {**base, **extra}
            status, body = http(
                "POST", f"{self.api}/accounts/{self.account_id}/messages",
                token=self.access_token, json_body=payload)
            new_id = dig(jparse(body), "messageId")
            tried.append(f"{extra}: HTTP {status}")
            if status == 200 and new_id:
                self.draft_id = str(new_id)
                p.ok(f"works with {extra}; draftId={self.draft_id}; "
                     f"tried {tried}")
                return p
        p.fail("no draft variant worked; tried " + " | ".join(tried))
        return p

    def p_move(self) -> Probe:
        p = Probe("PUT /updatemessage moveMessage")
        target = getattr(self, "draft_id", "") or \
            getattr(self, "sent_message_id", "")
        if not target:
            p.inconclusive("no movable message (drafts+send both failed)")
            return p
        trash_fid = None
        for name in ("Trash", "trash"):
            if name in self.folder_ids:
                trash_fid = self.folder_ids[name]
        if not trash_fid:
            p.inconclusive("no Trash folder")
            return p
        status, body = self._updatemessage(
            {"mode": "moveMessage", "destfolderId": int(trash_fid),
             "messageId": [int(target)]})
        if status == 200:
            self.moved_id = target
            p.ok(f"moved {target} to Trash (HTTP {status})")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)}")
        return p

    def p_delete(self) -> Probe:
        p = Probe("DELETE /folders/{fid}/messages/{mid} (→ trash)")
        target = getattr(self, "moved_id", "") or \
            getattr(self, "draft_id", "") or \
            getattr(self, "sent_message_id", "")
        if not target:
            p.inconclusive("no deletable message")
            return p
        fid = getattr(self, "view_folder_ids", {}).get(str(target))
        if not fid:
            sent = self.folder_ids.get("Sent")
            fid = sent
        if not fid:
            p.inconclusive("no folder id for target message")
            return p
        status, body = http(
            "DELETE", f"{self.api}/accounts/{self.account_id}/folders/"
                      f"{fid}/messages/{int(target)}",
            token=self.access_token)
        if status == 200:
            p.ok(f"deleted (→ trash) HTTP {status}")
        else:
            p.fail(f"HTTP {status}: {excerpt(body)}")
        return p

    # -- runner ---------------------------------------------------------

    def run(self) -> None:
        steps: list = [self.p_refresh_or_exchange]
        for step in steps:
            probe = step()
            self.probes.append(probe)
            mark = {"PASS": "\033[92m✓\033[0m", "FAIL": "\033[91m✗\033[0m",
                    "INCONCLUSIVE": "\033[93m?\033[0m",
                    "SKIP": "-"}[probe.verdict]
            print(f" {mark} {probe.verdict:<13} {probe.name}")
            if probe.detail:
                print(f"     {probe.detail}")
            if getattr(step, '__name__', '') == 'p_refresh_or_exchange' \
                    and self.access_token:
                steps.extend([
                    self.p_accounts, self.p_folders, self.p_view,
                    self.p_pagination, self.p_original, self.p_flags,
                    self.p_star, self.p_send, self.p_sent_filing,
                    self.p_attachment_upload, self.p_drafts, self.p_move,
                    self.p_delete,
                ])

    def write_notes(self) -> None:
        run_lines = [
            f"\n## Spike run {datetime.now(UTC).isoformat()} "
            f"(dc={self.dc})\n",
        ]
        for probe in self.probes:
            run_lines.append(f"- **{probe.verdict}** {probe.name}"
                             f"{' — ' + probe.detail if probe.detail else ''}")
        critical_fail = any(p.verdict == "FAIL"
                            for p in self.probes if p.critical)
        if critical_fail:
            run_lines.append("\n**GATE: FAIL** — a critical probe failed.\n")
        else:
            run_lines.append("\n**GATE: PASS** — all critical probes "
                             "succeeded.\n")
        with open(NOTES_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n".join(run_lines))
        print(f"\nAppended results to {os.path.normpath(NOTES_PATH)}")


def main() -> int:
    dc = os.environ.get("ZOHO_DC", "com")
    client_id = os.environ.get("ZOHO_CLIENT_ID", "")
    client_secret = os.environ.get("ZOHO_CLIENT_SECRET", "")
    refresh = os.environ.get("ZOHO_REFRESH_TOKEN", "")
    if not client_id or not client_secret:
        print("Set ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET "
              "(see docs/zoho-oauth-setup.md)")
        return 2
    spike = Spike(dc, client_id, client_secret, refresh or None)
    spike.run()
    spike.write_notes()
    gate_fail = any(p.verdict == "FAIL" for p in spike.probes if p.critical)
    return 1 if gate_fail else 0


if __name__ == "__main__":
    sys.exit(main())
