# zoho-bridge

Use a **Zoho Mail free-tier account with Nextcloud Mail** (or any IMAP/SMTP
client). Zoho paywalls IMAP/POP/SMTP on the free plan — webmail only — but
its **official REST API works on free accounts**. This bridge exposes that
API as a local IMAP server + SMTP submission endpoint, so Nextcloud Mail
connects to it like any normal mail server (Proton-Bridge pattern).

```
Nextcloud Mail ──IMAP:1143──▶ ┌───────────────────────────┐
                              │  zoho-bridge (podman)     │
Nextcloud Mail ─SMTP:10587──▶ │  pymap IMAP server        │──▶ Zoho Mail REST API
                              │  aiosmtpd submission      │    (OAuth 2.0)
web browser    ──:8000──────▶ │  sync poller + ops worker │
                              │  SQLite state             │
                              └───────────────────────────┘
```

**Verified live against a free-plan account** (2026-11-15, see
`docs/zoho-api-notes.md`): folder list, reading messages + attachments,
send, read/unread + star flags propagate both ways, UID MOVE/DELETE (→ Zoho
move / trash), folder create, drafts (saved via the send endpoint's
`mode: saveDraft`), sent-copy dedupe (Zoho auto-files Sent copies; may
rewrite Message-IDs — falls back to subject+time matching), IDLE fed by the
poller (Zoho has no push), UID stability across restarts (SQLite-backed UID
mapping).

**Known limitations:** **sending attachments is blocked by Zoho on the free
plan** (`UPLOAD_RULE_NOT_CONFIGURED`) — the bridge rejects submissions with
attachments rather than silently dropping them (reading attachments works);
no folder rename; custom-folder COPY is local-only; `\Answered`/`\Draft`
flags are local-only; polling latency (default 120 s) instead of true push;
unofficial use of the API on the free tier is an undocumented privilege — it
may change or break (not malicious use, low personal risk; a paid Mail Lite
plan is the zero-effort alternative).

## Setup

### 1. OAuth grant (one-time, ~10 min)

Follow **[docs/zoho-oauth-setup.md](docs/zoho-oauth-setup.md)**: register a
Self Client in the [Zoho API console](https://api-console.zoho.com), then run
the spike script — it mints the refresh token and **probes every endpoint the
bridge needs on your real account** (this is the phase 0 feasibility gate;
results land in `docs/zoho-api-notes.md`):

```
set ZOHO_CLIENT_ID=… & set ZOHO_CLIENT_SECRET=…
python scripts\spike\spike.py
```

### 2. Run the bridge (podman)

```bash
podman build -t zoho-bridge .

# /etc/zoho-bridge.env  (mode 600)
ZOHO_DC=com
ZOHO_CLIENT_ID=…
ZOHO_CLIENT_SECRET=…
ZOHO_REFRESH_TOKEN=…
BRIDGE_IMAP_USER=bridge
BRIDGE_IMAP_PASSWORD=…          # the login Nextcloud Mail will use
BRIDGE_SMTP_USER=bridge         # defaults to BRIDGE_IMAP_USER
BRIDGE_SMTP_PASSWORD=…          # defaults to BRIDGE_IMAP_PASSWORD

podman run -d --name zoho-bridge \
  -v zoho-bridge-data:/data \
  --env-file /etc/zoho-bridge.env \
  -p 1143:1143 -p 10587:10587 -p 127.0.0.1:8000:8000 \
  zoho-bridge
```

or with the provided quadlet unit (`zoho-bridge.container`) → systemd.
A self-signed TLS cert is generated on first start (`/data/bridge-*.pem`).

### 3. Connect Nextcloud Mail

- IMAP: `bridge-host.lan:1143`, **STARTTLS**, user/password from above
- SMTP: `bridge-host.lan:10587`, **STARTTLS**, same credentials
- Self-signed cert: Nextcloud admin setting
  *"Turn off mail server TLS-verification"* (Settings → Groupware), or
  mount your own cert via `CERT_FILE`/`KEY_FILE`
- Use the LAN hostname, not `localhost`, if Nextcloud runs elsewhere

For multiple Nextcloud users, configure the Mail app's **admin provisioning**
(Settings → Groupware → Mail) with the bridge host + the bridge credentials
as the password pattern — every user's account appears automatically.
(All provisioned users see the same Zoho mailbox; per-user mailboxes would
need one bridge instance per Zoho account.)

Health/status: `http://bridge-host:8000/healthz` and `/status` (JSON:
last sync, folders, pending/dead ops, raw-cache size).

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `ZOHO_DC` | `com` | data center: com/eu/in/com.au/jp/sa/ca/cn |
| `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` / `ZOHO_REFRESH_TOKEN` | — | OAuth grant (required) |
| `BRIDGE_IMAP_USER` / `BRIDGE_IMAP_PASSWORD` | `bridge` / — | IMAP login for clients (required) |
| `BRIDGE_SMTP_USER` / `BRIDGE_SMTP_PASSWORD` | = IMAP | SMTP login |
| `BRIDGE_DB_PATH` | `bridge.db` | SQLite state (persistent volume!) |
| `BRIDGE_POLL_SECONDS` | `120` | Zoho → bridge sync interval |
| `BRIDGE_PREFETCH` | `25` | newest messages whose bodies are pre-fetched per sync |
| `BRIDGE_FULL_SCAN_EVERY` | `10` | full folder sweep (deletions + stale flags) every N cycles; other cycles page only for new arrivals |
| `BRIDGE_RAW_CACHE_MB` | `512` | raw-message cache budget |
| `BRIDGE_SMTP_PORT` / `BRIDGE_SMTP_HOST` | `10587` / `0.0.0.0` | submission listener |
| `BRIDGE_HEALTH_PORT` / `BRIDGE_HEALTH_HOST` | `8000` / `127.0.0.1` | health/status HTTP (unauthenticated — keep loopback) |
| `BRIDGE_ALLOW_ATTACHMENT_LOSS` | `0` | send without attachments if upload endpoint fails (otherwise submission is rejected) |
| `CERT_FILE` / `KEY_FILE` | auto self-signed | TLS cert for STARTTLS |
| `BRIDGE_TLS_HOSTNAME` | host name | comma-separated SAN entries for the self-signed cert |

## Development

```bash
uv sync                                  # deps + editable install (uses uv.lock)
uv run ruff check .                       # lint
uv run pytest tests/ -q --ignore=tests/test_imap_e2e.py  # unit tests
uv run pytest tests/test_imap_e2e.py -q  # full E2E (bridge subprocess + fake Zoho, no account needed)
```

GitHub Actions runs ruff + both test tiers on Linux and Windows on every push
and PR (`.github/workflows/ci.yml`). `uv.lock` is committed for reproducible
dev/CI installs.

The E2E runs the **real pymap server as a subprocess** (`tests/bridge_boot.py`,
which also shims out pymap's Unix-only signal handlers on Windows) against an
in-thread fake of the Zoho API (`tests/fake_zoho.py`), driven with real
imaplib/smtplib clients. It reproduces the production topology exactly.

**Live check against your real account** (read + send + flags, safe — the
test mail it sends to yourself is easily deleted):

```
uv run python scripts/live_check.py
```

### Patched upstream issues (re-check on pymap upgrades)

- **`proxyprotocol` drops the connection-task reference**
  (`reader.py:_read_then_call` → `create_task` result discarded): tasks were
  reaped by the cyclic GC seconds in, killing live IMAP connections silently.
  Patched in `zoho_bridge/main.py` with a reference-keeping variant. The
  `proxy-protocol` package is a pinned direct dependency precisely because of
  this patch — re-verify `_read_then_call`'s signature on any bump.
- **`pymap.concurrent._AsyncioEvent.wait(timeout=…)` leaks one orphaned inner
  task per call** (observed on CPython 3.14) — the bridge avoids it by using
  a plain `asyncio.Event` for the sync wake.
- The default `cpu_subsystem` runs coroutine chunks on a private per-thread
  loop that deadlocks asyncio-bound backends — `BridgeConfig` forces the
  asyncio subsystem (harmless: the bridge never hashes passwords).

### Layout

```
src/zoho_bridge/
  config.py            env settings + pymap BridgeConfig
  context.py           BridgeContext: shared services handle
  mimeutil.py          shared MIME parsing (drafts + submission)
  zoho/client.py       REST client (OAuth refresh, retry, variant probes)
  zoho/models.py       response normalization
  state/store.py       SQLite: folders/UID map/flags/raw cache/outbox ops
  imap/backend.py      pymap backend plugin (MailboxSet/MailboxData/Message)
  imap/health.py       /healthz + /status
  smtp/handler.py      aiosmtpd submission → Zoho send
  poller.py            poller (Zoho→bridge) + ops worker (bridge→Zoho)
  main.py              entry point (+ upstream task-retention patch)
scripts/spike/spike.py phase 0 endpoint probe (stdlib-only)
docs/                  OAuth setup + verified-API notes
```

## Security & sync notes

- **Auth is TLS-gated.** pymap advertises `LOGINDISABLED` until STARTTLS, so
  credentials never cross the wire in cleartext to a non-loopback client. As a
  deliberate pymap behavior, **connections from `127.0.0.1`/`localhost` may
  authenticate without STARTTLS** (loopback is treated as trusted) — so don't
  run untrusted processes on the same host as the bridge, or don't expose
  1143/10587 on a shared machine.
- **health/status is unauthenticated** and now binds `127.0.0.1` by default
  (`BRIDGE_HEALTH_HOST`). The container sets it to `0.0.0.0` internally only so
  a host-side `127.0.0.1:8000:8000` publish can reach it; keep that publish
  host-local.
- **Self-signed cert** (new installs) carries a `subjectAltName` of
  `BRIDGE_TLS_HOSTNAME` (default: the host name) and `CA=FALSE`, so you *can*
  distribute the cert and keep TLS verification on instead of disabling it.
- **Sync cadence.** Each poll cycle normally pages a folder only until the
  first page of already-known messages (fast for idle folders). A full sweep
  every `BRIDGE_FULL_SCAN_EVERY` cycles re-reads everything to catch older
  flag changes and deletions made in webmail. Deletions propagate within that
  sweep window, not the per-cycle window.
