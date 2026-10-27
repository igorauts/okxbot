# Reader recovery reference — template ONLY; no automatic installation

**IMPORTANT:** This folder has no credentials and contains no installation hook. The actual Telegram entrypoint is `/root/okx/okx_telegram_monitor.py`, NOT `/opt/okx-grid-live/okx_telegram_monitor.py`. Only a specifically approved LIVE migration may change services or reload systemd.

Source of account credentials already used by the manager: `/root/okx/.okxbot_account.env`. The Reader receives this **same file** via `--credentials-file`. Do not create a Reader API key; do not echo keys. This service must run with the same verified OKX UID. Its credential parser requires owner UID matching and mode `0600` (or stricter). DO NOT remove the archived ZEC `.env` until live migration to the common source is explicitly verified.

## Expected paths

- Reader executable: `/opt/okx-grid-live/okx_position_reader_service.py`
- Account registry: `/root/okx/.okxbot_registry.json`
- Shared quota gate: `/root/okx/.account_positions_gate_v075.json` (same original global queue, do not clone)
- Socket: `/run/okx-position-reader/positions.sock`, parent 0700 and socket 0600
- Telegram runtime modules: `/root/okx/okx_telegram_monitor.py`, `/root/okx/okx_telegram_health.py` (verify effective `ExecStart` and import target before updating)
- Reader watcher executable: `/opt/okx-grid-live/okx_position_reader_watchdog.py` (must be copied during a future approved install, not now)

## Checklist before LIVE migration (not performed by this toolkit)

1. Inspect effective `systemctl cat/show` of current Reader and Telegram services, record exact executable paths, SHA256, PIDs, and store timestamped backups. Do not paste `.env` contents.
2. Confirm `/root/okx/.okxbot_account.env` is a regular non-symlink, root-owned private file and yields the same account UID as the present Reader. Stop if differing or uncertain.
3. Review generated engine, Reader, Telegram changes, run entire project `run_checks.sh` + isolated regression and verify engine SHA/manifest.
4. Deploy Reader unit, watchdog, Telegram files **separately**; no trading service restart until user separately approves a one-bot LIVE engine rollout. Do not copy `/opt/...` Telegram scripts onto `/root/okx/...` without matching source SHA and a targeted backup.
5. Start only explicitly approved components; validate that remaining live bots retain ownership/TP/fills, Reader IPC and safe fallback through existing gate; inspect Telegram for flood and missed incidents.
6. Rollback must restore exact previous Reader unit and code with previous credential source, plus same IPC contract, without replacing `/root/okx/<bot>/state` or any archive. Confirm trading fallbacks before restoring a failure mode.

## Watchdog semantics

- The watcher checks IPC `health` only (zero HTTP GET, zero exchange POST).
- It never starts an inactive/failed Reader and **never controls any trading engine**; systemd's own `Restart=on-failure` remains the primary crash recovery.
- Only a *still-active*, same-PID Reader that misses at least 3 health probes may be restarted; min 10 minutes between attempts, max 2 per hour.
- All state is local; if clock or state cannot be trusted it **does not restart**.
- Production testing of hangs/restarts requires separate explicit approval; offline unit tests are not an OKX LIVE failover claim.

## Client opt-in (source of truth, NOT a bot systemd drop-in)

Existing bot code parses `<bot>/.env` directly; its systemd unit does **not** use
`EnvironmentFile=` for this Reader configuration. Therefore adding a bot
`Environment=` drop-in would not change the bot's internally parsed `ENV`.
The two non-secret options below document the existing format. Preserve all
other credentials and parameters, never replace entire `.env` files:

```dotenv
OKX_POSITION_READER_SOCKET=/run/okx-position-reader/positions.sock
OKX_POSITION_READER_QUOTA_SCHEDULING=mixed-fifo
```

No `priority` ack. Every new bot must be validated against the actual Reader
registry and shared quota gate independently. Do not apply opt-ins to terminal
archives for the sake of compatibility.
