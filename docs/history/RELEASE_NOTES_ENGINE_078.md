# v0.7.78 — immutable engine identity repair

- Release, manager, engine: **0.7.78** (`engine` release).
- Restores historic engine `0.7.76` dependency pins byte-for-byte from tag `v0.7.76`.
- Introduces new engine `0.7.78` entry with the audited HTTP modules from tag `v0.7.77`.
- No changes to `okx_bot.py`, strategies, POST/cancel behavior, Reader or HTTP code.
- `okx_version.py` and `okx_manager_version.py` carry the new identity; both manifests repinned.
- `v0.7.77` retains its published historical exception; DO NOT rewrite its Git tag or asset.
- This ZIP is OFFLINE ONLY until targeted LIVE rollout with backup and rollback.

## UI version display correction

- `okxbot status` and `okxbot settings` now show SHA-pinned installed `ENGINE_VERSION` from each bot root.
- Active process identity and start time are checked before claiming a live engine version.
- Historical `config.json.version` and trading state are never rewritten.
- No changes to trading or HTTP modules, Reader, order/TP logic.
