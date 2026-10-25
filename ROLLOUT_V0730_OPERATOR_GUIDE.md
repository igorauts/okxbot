**Historical operator checklist from pre-release RC; not an instruction to repeat STOP/UPGRADE/START on instances already at v0.7.30. The reported first five upgrades passed exchange audits on 2026-10-25 UTC.**

# OKX Grid Bot v0.7.30 — historical controlled LIVE rollout record

## Source of truth
- Based on official v0.7.29 and compatible refactor offline RC2.
- Package + engine identity: **0.7.30 / 0.7.30**, `STATE_SCHEMA=1`.
- Existing 0.7.28 and new 0.7.30 engines are attested by SHA256 in `engine_manifest.json`.
- Current bots (subject to fresh verification): `near0721`, `btc0721`, `dood0721-2`, `zec0724`, `wld0728`; `sui0727` excluded.
- v0.7.29 is the immutable previous release; publishing this source release does not change VPS or authorize further LIVE stops.

## Invariants preserved
1. The same original shared account `/positions` quota/queue paths and schema 1. **8 requests / 2.1 s**; fills-history **9 / 2.1 s**. Mixed old/new processes share the same flock inode. Never unlink, rename or recreate quota files during rollover.
2. Telegram state schema 1, persisted alert outbox, PANIC/heartbeat incident state and offsets retained; original wire keys preserved.
3. The old stats JSON key `performance_v0724` is retained in dual-write, readers also accept `performance`.
4. Live positions, exchange orders, config, state and financial databases preserved; no wholesale bot shutdown.
5. Fail closed for unknown POST, PANIC/HARD, ownership and unresolved orders. An offline backup/rollout cannot guarantee future exchange availability or price coverage while the engine is stopped.
6. No secret `.env` contents are printed or added to the package.

## Preparation stage — NO LIVE changes
From **Mac** first copy the pinned ZIP to the Tokyo VPS via `scp` (use actual filename supplied with the artifact). On the VPS extract once under a dedicated `/srv/` directory. Supply the independently recorded ZIP SHA256 to `rollout_preflight.py`.

Preflight (read-only local files + `systemctl show`; no exchange requests, no service actions):

```bash
python3 -B /srv/okxbot-v0730-stage/rollout_preflight.py \
  --package /srv/okxbot_v0730_controlled_rollout_candidate.zip \
  --stage /srv/okxbot-v0730-stage \
  --sha256 '<exact candidate ZIP SHA256>' \
  --bot near0721 --bot btc0721 --bot dood0721-2 --bot zec0724 --bot wld0728
```

The preflight refuses: wrong staged bytes; unexpected source engine hash; custom account gate paths; unknown FIFO format; stale heartbeat; offline trading/observer/Telegram unit; terminal state; unresolved local trading intent. It **does not independently verify exchange positions or open orders**. A passing preflight must be reviewed against the current VPS before authorizing any stop.

## Once the operator specifically approves LIVE
The candidate `okx_live_upgrade.py` supports independent actions: `preflight`, `shared`, `telegram`, `bot`, `status`. Each requires `--package`, `--stage`, `--sha256`, `--from-engine`, `--bot`; `bot` also requires `--confirm-live-stop`. **Do not run them now without explicit separate LIVE authorization.**

Controlled sequence after explicit approval:

1. Review actual live state and GET-only exchange positions/orders; confirm the bot's ownership and known TP coverage. Check no unresolved POST/BUY/SELL. Choose one canary, *not automatically WLD or ZEC*.
2. Apply `shared` once, with its own complete backup. Confirm all untouched trading services are still active and sharing the original FIFO. No bot stop here.
3. Apply `telegram` once, preserving the original persisted Telegram schema/queues. Confirm `/status` and alert delivery.
4. Upgrade **one selected bot only**. The updater checks disk hash, systemd root, current local state, custom gate paths and pending intents *before stop*. Then stops that service, checks state again, backs up entire `state/` and the config/code, replaces engine/dependencies and version metadata, performs a GET-only exchange-order audit, and starts it only on success. Note: GET-only refers to *exchange methods*, not guaranteed local read-only financial journal updates.
5. Verify new PID, fresh heartbeat, real position/TP coverage, pending order ownership and Telegram notifications. If any uncertainty exists, leave that bot stopped for manual resolution; do not auto-restart or advance to another bot.
6. After a separate explicit confirmation for each additional bot, repeat. GitHub Release only after controlled LIVE and review.

**Critical:** A stopped bot's exchange orders may remain live, while virtual TP is unmonitored. Limit downtime and do not treat `systemctl active` as sufficient exchange reconciliation. The updater does not issue market orders or emergency commands itself.

## Compatibility limits / OPEN
- A new runtime being compatible on offline fixtures does not mean the actual VPS mix is safe: check gate-path overrides in every `.env` without leaking secrets and manager/account paths too.
- Unrecognized legacy engine generations are not trusted automatically, even if old version strings appear.
- Interruptions between shared-file replacements are not fully crash-atomic as a group. Use backups and inspection if interrupted; no unattended bulk deployment.
- The trade-engine's GET-only audit may populate local financial logs/SQLite. It requires a verified backup of the complete state. Do not call it a filesystem read-only operation.
- Current installed LIVE engine is understood to be 0.7.28, but the local preflight is authoritative for actual bytes/version at execution.
