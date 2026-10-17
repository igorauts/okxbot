# v0.7.9 — BUY Refill Priority / TP Quiet Cleanup

- Excess real SELL TP no longer blocks BUY refill after successful REST reconciliation.
- Real SELL TP 15/25/55 are legitimate temporary surpluses; cleanup one-by-one after 1.5 s of quiet.
- At 150 real TP, immediate one-by-one safety cleanup, no BUY capacity.
- Keep BUY target +5 buffer, no-churn, duplicate and pending-POST safeguards.
- Keep 5-order asynchronous TP headroom in BUY capacity planning.
- v079 read-only observer accepts 0.7.9 states and warns only above 150 TP.
- Existing v078 LIVE services are NOT updated by this builder.
- Separate preflight/audit and manual deployment are required.

- Release includes v0.7.9 monitor controller, manager compatibility fix and Telegram menu.
- 80 offline tests passed: v076 45, v078 13, v079 9, stress 13.
- PANIC_DONE automatic systemd cleanup and NEAR/ZEC/SUI: next release, NOT v0.7.9.
- Historical install.sh installs v0.7.3; DO NOT use it to deploy v0.7.9.
