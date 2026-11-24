# OKX Funding Collector

Independent, GET-only account statistics; no engine, Reader, Governor or Telegram changes.

- Polls once per hour at :05 UTC (24-hour overlap), daily 7-day reconciliation; initial backfill and gap recovery use the bills archive within available history.
- Uses the existing single-account credentials and registry. Counts nonterminal bots (including stopped bots) by instrument and creation timestamp. Terminal reports remain unchanged.
- SQLite WAL, exact signed Decimal amounts, billId deduplication, atomic snapshots, seven daily backups on the VPS. Retains prior sums on failures; quarantines incompatible records and isolates instrument-level problems. No funding-based trading decisions.
- Manager reads ready values locally and adds funding to the existing reconciled total PnL. Only the funding dot is colored in the curses viewer.

## Deployment (explicit operator action)

From this directory, review `systemd/paths.conf.example` against the manager's real registry and base directory. Use the existing credentials in the bot directories; never copy them into a release. Run offline tests before deployment: `python3 -B -m unittest discover -v`.

For a new installation only, as root, after verifying no conflicting files or services exist:

1. Create `/opt/okx-funding`, `/etc/okx-funding` and `/srv/okx-funding` with mode 0700. Copy the four `okx_funding_*.py` modules into `/opt/okx-funding` (mode 0600). Set `/etc/okx-funding/paths.conf` from the reviewed example (mode 0600).
2. From `/opt/okx-funding`, run `nice -n 19 ionice -c 3 python3 -B -E -s okx_funding_collector.py --base /root/okx --registry /root/okx/.okxbot_registry.json --systemd-dir /etc/systemd/system --data-dir /srv/okx-funding --mode backfill` (adjust paths to match the reviewed configuration). Verify PASS, published snapshots, backup, and SQLite integrity before proceeding. This makes authenticated GET requests only.
3. Install the two systemd units under `/etc/systemd/system` (0644), run `systemd-analyze verify` on both, then `systemctl daemon-reload`. Start only `okx-funding-collector.service` once and inspect its result and journal. Enable the timer only after success: `systemctl enable --now okx-funding-collector.timer`.

Existing installations: do not reset or replace the database. Do not overwrite an existing configuration or units blindly. The manager-only release must be installed using the project's normal verified maintenance procedure. Collector sources in this directory are identical to the deployed standalone files; root-level reader modules are identical copies for the manager. No automated deployment is performed by publishing this release.

The public fixture is synthetic (identifiers, instruments and amounts replaced); it retains the observed response shape, equal timestamps, signs and pagination pattern. Live history, credentials, logs and backups are not included. The benchmark is synthetic; run `python3 -B benchmark_funding.py` locally if desired.
