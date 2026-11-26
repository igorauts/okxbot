# OKX Grid Bot v0.7.77

Combined HTTP transport + Reader-first manager startup.

- Package/manager release: 0.7.77; engine stays 0.7.76; existing LIVE engine processes are not upgraded by publication.
- HTTP: approved live-tested `okx_api.py` and `okx_latency.py` as deployed on six bots.
- New bot: Reader registration now runs before read-only startup audit.
- Maintains second Reader registration gate after explicit START confirmation.
- Never auto-starts, places, or cancels any live order during build or publication.
- `okx_source_bundle.json`, `engine_manifest.json`, transport AST references and tests updated.
- Offline run_checks required before GitHub publication; no tests during publisher.

- Interactive START confirmation: direct /dev/tty UTF-8 decoding after curses; invalid input cancels safely. Existing engine and POST behavior untouched.
