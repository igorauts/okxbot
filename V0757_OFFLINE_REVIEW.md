# v0.7.57 — OFFLINE REVIEW ONLY (2026-11-04)

NOT an installer. NO LIVE changes, no OKX calls, no Github push.

SOURCE: pinned v0.7.56 installable ZIP SHA256 0e65107595883cb476aa2d3496979ca30cd659949e8e54167679ebaf2d734d2e, exact engine SHA256 27ea75bcdf75cc4869e13c67e2992f45c5c83f9cf4d6ec297ed394f296738370.
CANDIDATE_ENGINE_SHA256=04c2fb1ebddad3c10274e3a81cb1bb944483ee176e0203e40c295bc6230b3400

STRICT SCOPE:
1. Elective CANCEL-only idle TLS rotation (>15s) before *one* POST, with local sanitized nonblocking telemetry. New opt-in on exactly one source call site; GET and all other trading POST paths unchanged. Uncertain cancellation is NOT retried.
2. Per-bot /latency CLI uses only verified running systemd grid units, dynamic WorkingDirectory, active PID, no hardcoded bot list. Telegram calls source-controlled sibling CLI instead of versioned /srv CLI.
3. CHANGELOG recovered 0.7.50–0.7.53 history and added candidate/historical 0.7.54–0.7.56 entries.

EXACT CHANGED/ADDED FILES (11):
- CHANGELOG.md
- engine_manifest.json
- okx_api.py
- okx_bot.py
- okx_latency.py
- okx_per_bot_speed_readonly.py
- okx_telegram_monitor.py
- okx_version.py
- run_checks.sh
- tests/test_v0757_idle_cancel_socket.py
- tests/test_v0757_latency_discovery.py

OFFLINE CHECKS:
- Existing development run_checks.sh, plus two focused v0.7.57 modules: PASS, shell exit 0. (Full suite runs only during candidate development. GitHub publisher MUST NOT run tests.)
- 44 focused HTTP/CANCEL/CLI/legacy tests PASS, no exchange/network calls.
- AST parse and Python compile of modified modules PASS.
- All other 183 old source paths were byte-identical to pinned baseline.

NOT COVERED:
- No LIVE test of stale TLS against OKX (requires separately approved canary).
- Source-controlled CLI calls systemctl in LIVE; development test uses fake services and directories.
- No 7-bot LIVE rollout, no financial audit and no GitHub publication authorized/performed.
- Separate deployment package/installer must attest actual VPS source and stage per-engine + Telegram monitor explicitly, sequential fail-closed (BTC last).
