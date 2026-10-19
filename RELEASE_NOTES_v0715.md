# OKX Grid Bot v0.7.15 — Telegram + CLI manager

Telegram: agreed compact HTML `/status`, `/latency` and `/latency 8h`
summaries, `/help`. One final MARKET SELL notification after verified
net accounting, including estimated LIMIT TP net and execution delta.
Incident, partial-fill, BUY 0/N and BUY-restored alerts with deduplication.

CLI manager: live instances by creation time; read-only archive by final
stop time; Telegram monitor; account management. The local monitor
`service_active` hotfix is preserved.

Only manager and Telegram Python files change: trading engine, engine
manifest, trading services, bot states and trading logic stay at v0.7.14.

LIMIT TP fee is hypothetical; 0/N is observable only when the existing engine
journal emits a state line. Fill timestamp may mix local/OKX clock domains.
