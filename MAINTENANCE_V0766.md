# v0.7.66 — Dynamic Ticker Wizard (Manager Maintenance)

RELEASE_VERSION=0.7.66
RELEASE_KIND=maintenance
ENGINE_VERSION=0.7.64

## Changes
- Replace fixed ticker presets with dynamic discovery of active linear USDT-SWAP contracts from OKX.
- Support partial ticker input and exact symbols; disambiguate multiple matches before confirming.
- Confirm full instrument identifier, BID and already-supported contract parameters / fees.
- Show the rolling 24-hour high/low at the upper-grid boundary prompt using the existing ticker response (no extra request).
- Suggest a unique bot name derived from the ticker and manager release version; permit editing.
- Retain the existing GRID numeric wizard, validation and persistence contracts.

## Validation and scope
- Offline wizard contract and UI tests completed by the two installers before LIVE installation.
- Operator manually verified ticker selection, wizard flow, daily range and default name on LIVE VPS.
- GitHub publisher validates exact LIVE source byte parity and reproducible transformations; no tests at publish time.
- No trading-engine upgrade; existing engine identity remains 0.7.64.
- No OKX API calls or bot/service restarts during GitHub publishing.
