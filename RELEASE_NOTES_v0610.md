# OKX Grid Bot v0.6.10

**Safety and monitoring update, based on v0.6.9.**

The engine now attaches OKX-supported `expTime` to new BUY and recovery TP order POST requests and stores the deadline before issuing the network write. Lost acknowledgements are reconciled by unique client ID and fresh exchange history. After the deadline, it waits for complete history evidence across three separated checks; it never blindly resends the unknown request. v0.6.9 ambiguous requests without an original expiry remain unresolved rather than being guessed away.

Virtual TP maintenance can continue for an independently verified cycle even if another cycle has an uncertain TP. The uncertain SELL quantity remains reserved, preventing duplicate orders. No BUY is placed during a global unresolved state.

Optional standalone read-only Telegram monitor (one for ETH and SOL): critical alerts, daily digest, `/status`, and systemd health reporting. Telegram failure never blocks trading. Configure bot token and authorized chat ID before enabling the monitor.

All obsolete WEEX naming has been removed from the engine, including `format_okx_report` in place of the old formatter name. The report format and accounting values are unchanged. The repository manager also needs its supplied v0.6.10 update before deployment.

**No speed/batch/amend work** was included. Those remain planned for v0.7.0.
