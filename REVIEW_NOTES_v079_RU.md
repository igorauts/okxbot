# v0.7.9 extra safety review

When live real TP > target the pre-BUY maintain_virtual_tp call is skipped.
The independent local coverage_audit(st,pending,actual) is now checked
after all_resolved and before any RUN BUY, so failures stop trading.
No trading order method or virtual_escape was changed.
