# Zoho OAuth setup (one-time, ~10 minutes)

The bridge talks to the **official Zoho Mail REST API** using an OAuth 2.0
refresh token. On Zoho's free tier IMAP/POP/SMTP are paywalled, but the REST
API works (verified by multiple independent projects). You register your own
API client so nothing goes through third parties.

## 1. Register a Self Client

1. Go to <https://api-console.zoho.com> and sign in with the Zoho account that
   owns the mailbox (US DC: mail.zoho.com — if you use zoho.eu/.in/.com.au etc.
   set `ZOHO_DC` accordingly in every later step).
2. Click **Add Client** → choose **Self Client** → give it any name/description
   (e.g. "zoho-bridge").
3. On the client's page copy the **Client ID** and **Client Secret**.
4. Go to the **API Credentials** / **Generate Code** tab:
   - The **Scope** field starts **empty** — "Generate Code" fails with an
     unhelpful error until you paste scopes into it. Paste exactly this
     (one line, comma-separated, no spaces):

     ```
     ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.folders.CREATE,ZohoMail.messages.READ,ZohoMail.messages.CREATE,ZohoMail.messages.UPDATE,ZohoMail.messages.DELETE,ZohoMail.drafts.CREATE
     ```

   - Time: 10 minutes. Description: "zoho-bridge grant".
   - Click **Create Code** and copy the **one-time grant code** (looks like
     `1000.xxxxxxxx.xxxxxxxx`).
   - If it rejects a specific scope name, remove that scope and retry — the
     spike output will show which bridge operations then need it re-added.

## 2. Exchange the code for a refresh token

Run the spike script, which does the exchange and then probes every endpoint
the bridge needs (phase 0 feasibility gate):

```
set ZOHO_DC=com                       (com/eu/in/com.au/jp ... - omit for com)
set ZOHO_CLIENT_ID=1000.XXXXXXXXXXXX
set ZOHO_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
python scripts\spike\spike.py
```

When prompted, paste the grant code. The script prints a **refresh token** —
store it in the bridge's env file. It then walks the whole API surface
(accounts, folders, message listing/pagination, raw message download, send,
attachments, drafts, read/unread/star, move) and writes results to
`docs/zoho-api-notes.md`.

## What the scopes are for

| Scope | Used by the bridge for |
|---|---|
| `ZohoMail.accounts.READ` | discovering your account id |
| `ZohoMail.folders.READ` | IMAP LIST/SELECT (folder list) |
| `ZohoMail.folders.CREATE` | IMAP CREATE of folders (optional feature) |
| `ZohoMail.messages.READ` | reading messages / bodies / attachments |
| `ZohoMail.messages.CREATE` | SMTP submission → send |
| `ZohoMail.drafts.CREATE` | IMAP APPEND into Drafts |

## Security notes

- The refresh token grants **full read + send** access to that mailbox — keep
  the env file mode 600 and never commit it.
- Everything stays between your machine and Zoho: the bridge uses no third
  party service.
- You can revoke the grant any time by deleting the Self Client in
  <https://api-console.zoho.com>.
