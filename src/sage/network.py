"""
Async network connectivity monitor for Sage.

Performs lightweight TCP probes (no HTTP overhead) on startup
and re-checks periodically.

Usage (during lifespan):

    monitor = NetworkMonitor(settings.network)
    await monitor.start()

    # Later …
    if monitor.online:
        ...

    await monitor.stop()
"""

from __future__ import annotations

import asyncio

import structlog

from sage.config import NetworkSettings

log = structlog.get_logger(__name__)

# Probe multiple endpoints in parallel to reduce false negatives on restricted networks.
_PROBE_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
    ("1.0.0.1", 443),
)


async def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        _ = reader
        writer.close()
        await writer.wait_closed()
        return True
    except (TimeoutError, OSError):
        return False


async def check_internet(timeout: float = 2.0) -> bool:
    """Check internet reachability via parallel TCP probes to known public endpoints."""
    probes = [
        asyncio.create_task(_tcp_probe(host, port, timeout), name=f"net-probe-{host}-{port}")
        for host, port in _PROBE_ENDPOINTS
    ]

    try:
        for completed in asyncio.as_completed(probes):
            if await completed:
                return True
        return False
    finally:
        for probe in probes:
            if not probe.done():
                probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)


class NetworkMonitor:
    """Background service that tracks internet connectivity state."""

    __slots__ = (
        "_online",
        "_force_offline",
        "_interval",
        "_timeout",
        "_task",
    )

    def __init__(self, cfg: NetworkSettings) -> None:
        self._online: bool = False
        self._force_offline: bool = cfg.force_offline
        self._interval: int = cfg.check_interval
        self._timeout: float = cfg.timeout
        self._task: asyncio.Task[None] | None = None

    @property
    def online(self) -> bool:
        if self._force_offline:
            return False
        return self._online

    async def start(self) -> None:
        """Run an initial connectivity check and spawn the background poller."""
        if self._force_offline:
            log.info("network_forced_offline")
            return
        self._online = await check_internet(self._timeout)
        log.info("network_initial_check", online=self._online)
        self._task = asyncio.create_task(self._poll(), name="network-monitor")

    async def stop(self) -> None:
        """Cancel the background poller gracefully."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        log.info("network_monitor_stopped")

    async def _poll(self) -> None:
        """Periodically re-check connectivity and log transitions."""
        while True:
            await asyncio.sleep(self._interval)
            new_state = await check_internet(self._timeout)
            if new_state != self._online:
                self._online = new_state
                log.info(
                    "network_state_transition",
                    online=new_state,
                    detail="online" if new_state else "offline",
                )
