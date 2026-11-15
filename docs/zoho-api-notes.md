# Zoho Mail REST API — verified endpoint notes

All shapes below verified **live against a free-plan account** (DC=com,
2026-11-14/15) via `scripts/spike/spike.py` and `scripts/live_check.py`
(bridge subprocess against the real API: folders, read, UNSEEN, SMTP send →
INBOX arrival via poller, Sent dedupe, flag write-through — all PASS).
Evidence runs appended below.

## Scopes (Generate Code field; comma-separated, no spaces)

```
ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.folders.CREATE,ZohoMail.messages.READ,ZohoMail.messages.CREATE,ZohoMail.messages.UPDATE,ZohoMail.messages.DELETE
```

- **`ZohoMail.drafts.CREATE` is rejected as invalid by the console** — there
  is no drafts scope; drafts save via the send endpoint (`mode: saveDraft`).

## Auth

- `POST https://accounts.zoho.{dc}/oauth/v2/token` — `refresh_token` grant;
  header `Authorization: Zoho-oauthtoken <token>`
- the grant-code exchange response may omit `access_token` — mint one via
  the refresh grant right after

## Verified endpoints

| Operation | Call | Notes |
|---|---|---|
| Accounts | `GET /api/accounts` | `data: [{accountId, primaryEmailAddress}]` |
| Folders | `GET /api/accounts/{aid}/folders` | flat `data: [...]`; name field is **`folderName`**; also `folderId`, `folderType`, `imapAccess`, `path` |
| List messages | `GET /api/accounts/{aid}/messages/view?folderId=…&limit=N&start=N` | `start` offsets work; ids are **numeric strings** |
| Raw message | `GET /api/accounts/{aid}/messages/{mid}/originalmessage` | **content nested under `data.content`** |
| Send | `POST /api/accounts/{aid}/messages` | JSON: `fromAddress`, `toAddress`, `subject`, `content`, `mailFormat` (plaintext/html), `encoding`, `askReceipt: "no"`; **auto-files copy into Sent**; returns `data.messageId` |
| Save draft | `POST /api/accounts/{aid}/messages` | same body + **`"mode": "saveDraft"`** (no dedicated drafts endpoint) |
| Mark read/unread | `PUT /api/accounts/{aid}/updatemessage` | `{"mode": "markAsRead"/"markAsUnread", "messageId": [<long>…]}` |
| Star (flag) | `PUT /api/accounts/{aid}/updatemessage` | `{"mode": "setFlag", "flagid": "important"/"flag_not_set", "messageId": […]}` |
| Move | `PUT /api/accounts/{aid}/updatemessage` | `{"mode": "moveMessage", "destfolderId": <long>, "messageId": […]}` (+ optional `folderId` + `isFolderSpecific: true`) |
| Delete (→ trash) | `DELETE /api/accounts/{aid}/folders/{folderId}/messages/{mid}` | per-message; `expunge=false` default = move to Trash |
| Create folder | `POST /api/accounts/{aid}/folders` | `{"folderName": …}` → `data.folderId` |
| Delete folder | `DELETE /api/accounts/{aid}/folders/{fid}` | |
| Attachment upload | `POST /api/accounts/{aid}/messages/attachments` | **BLOCKED on free plan: 404 `UPLOAD_RULE_NOT_CONFIGURED`** — bridge rejects submissions with attachments unless `BRIDGE_ALLOW_ATTACHMENT_LOSS=1`; attachment *reading* works via raw MIME |

## View field semantics (verified by diffing around mutations)

- `status`: `"1"` = read, `"0"` = unread (`status2` is unrelated — stays `"0"`)
- `flagid`: `"important"` = starred, `"flag_not_set"` = unstarred
- `receivedTime` / `sentDateInGMT`: epoch-milliseconds as strings
- `messageId`: numeric string (long in mutation payloads)
- others: `subject`, `summary`, `fromAddress`, `sender`, `toAddress`,
  `ccAddress`, `size`, `hasAttachment`, `hasInline`, `priority`, `folderId`,
  `calendarType`

## Run log

## Spike run 2026-11-15T01:48:35.583562+00:00 (dc=com)

- **PASS** OAuth: get access token — authorization_code grant ok, refresh token minted

**GATE: PASS** — all critical probes succeeded.

## Spike run 2026-11-15T01:49:28.530499+00:00 (dc=com)

- **PASS** OAuth: get access token — refresh_token grant ok

**GATE: PASS** — all critical probes succeeded.

## Spike run 2026-11-15T01:50:02.669253+00:00 (dc=com)

- **PASS** OAuth: get access token — refresh_token grant ok

**GATE: PASS** — all critical probes succeeded.

## Spike run 2026-11-15T01:50:40.659981+00:00 (dc=com)

- **PASS** OAuth: get access token — refresh_token grant ok
- **PASS** GET /api/accounts (list accounts) — account id=1234567890123456789 address=user@example.test (1 account(s))
- **PASS** GET /api/accounts/{id}/folders — 11 folders: [None, None, None, None, None, None, None, None, None, None, None]…
- **INCONCLUSIVE** GET /api/accounts/{id}/messages/view (free plan read) — no Inbox folder id known
- **INCONCLUSIVE** messages/view pagination (limit>50 / start param) — no Inbox folder id known
- **INCONCLUSIVE** GET /messages/{id}/originalmessage (raw MIME) — no message id from view probe
- **INCONCLUSIVE** mark as read/unread (store \Seen) — no message id from view probe
- **INCONCLUSIVE** star/flag (store \Flagged) — no message id from view probe
- **PASS** POST /messages (SMTP send path) — sends mail to yourself — sent; response: {"status":{"code":200,"description":"success"},"data":{"askReceipt":"no","subject":"[zoho-bridge spike] plain send 20261115T015036Z","messageId":"1794707437841138400","fromAddress":"user@example.test.co…
- **INCONCLUSIVE** does send auto-file into the Sent folder? — no Sent folder in: [None]
- **FAIL** attachment upload (SMTP send w/ attachments) — HTTP 404: {"data":{"errorCode":"UPLOAD_RULE_NOT_CONFIGURED"},"status":{"code":404,"description":"Invalid Input"}}
- **FAIL** drafts create (IMAP APPEND → Drafts) — HTTP 404: {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"code":404,"description":"Invalid Input"}}
- **INCONCLUSIVE** move message between folders — no draft to move (drafts probe failed)
- **INCONCLUSIVE** delete message (EXPUNGE path) — no draft to delete (drafts probe failed)

**GATE: PASS** — all critical probes succeeded.

## Spike run 2026-11-15T01:51:35.077999+00:00 (dc=com)

- **PASS** OAuth: get access token — refresh_token grant ok
- **PASS** GET /api/accounts (list accounts) — account id=1234567890123456789 address=user@example.test (1 account(s))
- **PASS** GET /api/accounts/{id}/folders — 11 folders; fields=['HIDE', 'URI', 'VW', 'folderIcon', 'folderId', 'folderName', 'folderType', 'imapAccess', 'isArchived', 'path']; first={"path": "/Inbox", "VW": true, "HIDE": false, "isArchived": 0, "folderIcon": "default", "folderName": "Inbox", "imapAccess": true, "folderType": "Inbox", "URI": "https://mail.zoho.com/api/accounts/884…
- **PASS** GET /api/accounts/{id}/messages/view (free plan read) — 5 messages; message fields: ['calendarType', 'ccAddress', 'flagid', 'folderId', 'fromAddress', 'hasAttachment', 'hasInline', 'messageId', 'priority', 'receivedTime', 'sender', 'sentDateInGMT', 'size', 'status', 'status2', 'subject', 'summary', 'toAddress']
- **PASS** messages/view pagination (limit>50 / start param) — limit=100 returned 6 (window cap = 50); start=5 returned DIFFERENT (offset works)
- **FAIL** GET /messages/{id}/originalmessage (raw MIME) — HTTP 200: {"status":{"code":200,"description":"success"},"data":{"messageId":1794707438970153000,"content":"Delivered-To: user@example.test\r\nReturn-Path: <user@example.test>\r\nARC-Seal: i=1; a=rsa-sha256; t=1794707438; cv=none; \r\n\td=zohomail.com; s=zohoarc; \r\n\tb=F0V3wk0UFY45laNy/F8ItkfsvmPCAqPPJzSDZ16ailuo6o/kkGCIgexwqRng1zsc5mS6goki31uNsJiLQpFFBYA/46zcemxGaglajWx3np6jakllrNolLk7KxlvgqUOpFo0DEK…
- **PASS** mark as read/unread (store \Seen) — POST …/messages/markAsRead: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"code":404,"descriptio…; POST …/messages/markAsRead: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"code":404,"descriptio…; POST …/messages/markAsUnread: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"code":404,"descriptio…; POST …/messages/markAsUnread: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"code":404,"descriptio…; restore HTTP 404
- **FAIL** star/flag (store \Flagged) — no star variant returned 200; tried messages/addStar: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"c… | messages/flag: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"c… | messages/markAsFlagged: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"c…
- **PASS** POST /messages (SMTP send path) — sends mail to yourself — sent; response: {"status":{"code":200,"description":"success"},"data":{"askReceipt":"no","subject":"[zoho-bridge spike] plain send 20261115T015129Z","messageId":"1794707490817155300","fromAddress":"user@example.test.co…
- **PASS** does send auto-file into the Sent folder? — sent message 1794707490817155300 present in Sent (match by messageId)
- **FAIL** attachment upload (SMTP send w/ attachments) — HTTP 404: {"data":{"errorCode":"UPLOAD_RULE_NOT_CONFIGURED"},"status":{"code":404,"description":"Invalid Input"}}
- **FAIL** drafts create (IMAP APPEND → Drafts) — HTTP 404: {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"code":404,"description":"Invalid Input"}}
- **FAIL** move message between folders — /accounts/1234567890123456789/messages/move: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"code":404,"descriptio… | /accounts/1234567890123456789/messages/1794707490817155300/move: HTTP 404 {"data":{"errorCode":"URL_RULE_NOT_CONFIGURED"},"status":{"code":404,"descriptio…
- **FAIL** delete message (EXPUNGE path) — POST /accounts/1234567890123456789/messages/delete: HTTP 404 {"data":{"errorCode":"INVALID_METHOD"},"status":{"code":404,"description":"Inval… | DELETE /accounts/1234567890123456789/messages/1794707490817155300: HTTP 404 {"data":{"errorCode":"INVALID_METHOD"},"status":{"code":404,"description":"Inval…  (note: draft may still exist in Trash — delete manually)

**GATE: FAIL** — a critical probe failed; bridge cannot proceed on this account until resolved.

## Spike run 2026-11-15T01:55:54.125086+00:00 (dc=com)

- **PASS** OAuth: get access token — refresh_token grant ok
- **PASS** GET /api/accounts (list accounts) — account id=1234567890123456789 address=user@example.test
- **PASS** GET /api/accounts/{id}/folders — 11 folders; fields=['HIDE', 'URI', 'VW', 'folderIcon', 'folderId', 'folderName', 'folderType', 'imapAccess', 'isArchived', 'path']; first={"path": "/Inbox", "VW": true, "HIDE": false, "isArchived": 0, "folderIcon": "default", "folderName": "Inbox", "imapAccess": true, "folderType": "Inbox", "URI": "https://mail.zoho.com/api/accounts/1234567890123456789/fol…
- **PASS** GET /messages/view (free plan read) — 5 messages; fields=['calendarType', 'ccAddress', 'flagid', 'folderId', 'fromAddress', 'hasAttachment', 'hasInline', 'messageId', 'priority', 'receivedTime', 'sender', 'sentDateInGMT', 'size', 'status', 'status2', 'subject', 'summary', 'toAddress']; first={"summary": "Phase 0 spike of zoho-bridge. Safe to delete.", "sentDateInGMT": "1794736290000", "calendarType": 0, "subject": "[zoho-bridge spike] plain send 20261115T015129Z", "messageId": "1794707492965153000", "flagid": "flag_not_set", "status2": "0", "priority": "3", "hasInline": "false", "toAddr…
- **PASS** messages/view pagination (start param) — start=5 returned DIFFERENT (offset works)
- **PASS** GET /messages/{id}/originalmessage (raw MIME) — raw MIME 2197 chars (nested under data), starts: 'Delivered-To: user@example.test\r\nReturn-Path: <ben@whatupg'
- **PASS** PUT /updatemessage markAsRead / markAsUnread — markAsRead HTTP 200, markAsUnread HTTP 200 (message left unread)
- **PASS** PUT /updatemessage flag (star = 'important') — setFlag/flagid: HTTP 200 {"status":{"code":200,"description":"success"}}
- **PASS** POST /messages (send) — sent; messageId=1794707750275155100
- **PASS** does send auto-file into the Sent folder? — sent copy present in Sent (match by messageId)
- **FAIL** attachment upload (POST /messages/attachments) — HTTP 404: {"data":{"errorCode":"UPLOAD_RULE_NOT_CONFIGURED"},"status":{"code":404,"description":"Invalid Input"}} (bridge degrades: rejects submissions with attachments)
- **PASS** save draft (POST /messages with draft mode?) — works with {'mode': 'saveDraft'}; draftId=1794707754219155000; tried ["{'mode': 'saveDraft'}: HTTP 200"]
- **PASS** PUT /updatemessage moveMessage — moved 1794707754219155000 to Trash (HTTP 200)
- **PASS** DELETE /folders/{fid}/messages/{mid} (→ trash) — deleted (→ trash) HTTP 200

**GATE: PASS** — all critical probes succeeded.
