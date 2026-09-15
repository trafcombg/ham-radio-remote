"""Shared constants for the control (PTT/status) channel's connection
health check — client and server both send a keepalive ping and time out
a read the same way, so a silently-dead connection (no clean TCP FIN/RST
— a WiFi/NAT drop) gets noticed and cleaned up on both ends instead of
both sides waiting forever for data that will never arrive."""

KEEPALIVE_INTERVAL_S = 10.0
READ_TIMEOUT_S = 35.0  # a few missed keepalives, not a hair trigger
