# Operator tools (not installed automatically)

Run from a reviewed source package. Existing VPS copies are not deleted or relocated by this package. The 230ms Reader audit is historical/diagnostic only; current Reader pacing is unchanged. The retired shared installer and single-transition upgrader under tests/legacy/ are regression fixtures, not deployment paths.

`export_component.py funding|reader --out NEW_DIRECTORY` assembles verified copies from the canonical source files for later explicit deployment; it never writes an existing LIVE directory or restarts services.
