# v0.7.74 — Manager maintenance and Reader pacing 260ms

- Manager/package version: 0.7.74, engine stays pinned at 0.7.72.
- Manager identity separated from the engine's SHA-pinned okx_version.py.
- Fixed source-bundle attestation so new/clone can use unchanged engine with newer manager.
- Reader matches the confirmed LIVE 260ms / two-permit pacing SHA256
  92d88a3c92d2be163c65d84aa37c7cc59246b2881ec48294728d1f5aa7761a68. Existing hard 9 GET / 2.05s quota is unchanged.
- Updated abandoned-waiter test to accept no FIFO queue file for already dead caller.
- EMERGENCY, EXIT, B1/B2/B3, fallback, trade strategy and running trading engines unchanged.
- Manager-only LIVE deployment; existing bot process PIDs must remain unchanged.
- Full offline run_checks must PASS BEFORE the release is sealed for GitHub publish.
- No test execution during publication. No claim of proved LIVE speed improvement at 260ms.
