"""Bridge-level liveness proxy (Tier 4.5, Option 3b).

Watches the Reticulum daemon from *inside* the bridge process and reports
liveness two ways:

1. **systemd watchdog** — sends ``sd_notify(WATCHDOG=1)`` on a timer. With
   ``Type=notify`` + ``WatchdogSec`` on the service, systemd kills and
   ``Restart=on-failure`` restarts the bridge if pings stop.
2. **idle-heartbeat marker** — writes ``~/.hermes/.reticulum-idle-heartbeat``
   (mtime-authoritative) each tick, for ``/status`` to read and for
   post-mortem.

Why this is needed: ``Restart=on-failure`` only catches a process that
*exits* non-zero. A bridge whose RNS daemon has wedged — or whose GIL is held
by a stuck RNS C call — stays "running" (PID alive) so systemd never acts.
This thread probes RNS's actual liveness, not just "the process is alive",
and stops pinging when RNS wedges, so the watchdog fires. A total process
freeze (GIL held) also stops pinging, so it is caught too.

The probe is injectable for tests; the default reads
``RNS.Transport.interface_last_jobs`` (refreshed by RNS's job loop every
~5s).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time

logger = logging.getLogger("hermes_reticulum.liveness")

# Marker file: mtime-authoritative freshness signal (consistent with the
# turn-alive marker in hermes_client.py). Env-overridable.
HEARTBEAT_FILE = os.path.expanduser(
    os.getenv("HERMES_BRIDGE_HEARTBEAT_FILE", "~/.hermes/.reticulum-idle-heartbeat")
)

# Probe + notify period (seconds).
LIVENESS_INTERVAL = float(os.getenv("HERMES_BRIDGE_LIVENESS_INTERVAL", "30"))

# RNS job loop refreshes interface_last_jobs every ~5s. Healthy = refreshed
# within this window. Wedged = stale past it. Generous (6 missed refreshes)
# so a momentary GIL contention can't false-positive.
RNS_PROBE_MAX_AGE = float(os.getenv("HERMES_BRIDGE_RNS_PROBE_MAX_AGE", "30"))


def default_rns_probe() -> bool:
    """True if RNS's job loop is actively refreshing.

    Reads ``RNS.Transport.interface_last_jobs`` — a timestamp RNS touches
    every ``interface_jobs_interval`` (5s) seconds. Healthy: refreshed
    within :data:`RNS_PROBE_MAX_AGE`. Before RNS is constructed it is 0.0
    (never set) → stale → False.
    """
    try:
        import RNS

        last = float(getattr(RNS.Transport, "interface_last_jobs", 0.0))
    except Exception:
        return False
    if last <= 0.0:
        return False
    age = time.time() - last
    return 0.0 <= age <= RNS_PROBE_MAX_AGE


def sd_notify(message: str) -> bool:
    """Send a systemd notification (``WATCHDOG=1`` / ``READY=1``).

    No-op (returns False) when not running under a ``Type=notify`` unit
    (no ``NOTIFY_SOCKET``), e.g. foreground/dev runs. Never raises.
    """
    path = os.environ.get("NOTIFY_SOCKET", "")
    if not path:
        return False
    if path.startswith("@"):  # abstract namespace socket
        path = "\0" + path[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(message.encode("utf-8"), path)
        finally:
            sock.close()
        return True
    except OSError as e:
        logger.debug("sd_notify(%s) failed: %s", message, e)
        return False


class BridgeLiveness:
    """In-process liveness proxy: RNS probe + sd_notify + heartbeat marker.

    Start it after the bridge is up (RNS constructed) and stop it on
    shutdown. Runs a single daemon thread; safe to start/stop once.
    """

    def __init__(
        self,
        heartbeat_file: str = HEARTBEAT_FILE,
        interval: float = LIVENESS_INTERVAL,
        rns_probe=None,
        notify=None,
    ):
        self.heartbeat_file = heartbeat_file
        self.interval = max(1.0, float(interval))
        self._rns_probe = rns_probe or default_rns_probe
        self._notify = notify or sd_notify
        self._running = False
        self._thread: threading.Thread | None = None
        # Last probe outcome, exposed for /status and tests.
        self._last_rns_healthy = False
        self._last_tick = 0.0
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Begin liveness monitoring; send READY + first WATCHDOG immediately.

        The first WATCHDOG is sent *unconditionally* (not gated on the probe)
        so a just-started bridge — whose ``interface_last_jobs`` may not yet
        have been refreshed — always resets the watchdog clock.
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, name="bridge-liveness", daemon=True
            )
        self._notify("READY=1")
        self._tick(healthy=True)  # immediate first ping
        self._thread.start()
        logger.info(
            "Bridge liveness proxy started (interval=%.0fs, marker=%s)",
            self.interval, self.heartbeat_file,
        )

    def stop(self) -> None:
        """Stop the proxy. Marker left in place: its mtime is the accurate
        "last seen" signal for a now-stopped bridge."""
        with self._lock:
            self._running = False
        t = self._thread
        if t is not None:
            t.join(timeout=self.interval + 1)

    # ── internals ────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            # Sleep in small steps so stop() is responsive.
            deadline = time.time() + self.interval
            while self._running and time.time() < deadline:
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
            if not self._running:
                break
            try:
                healthy = self._rns_probe()
            except Exception as e:
                logger.warning("liveness probe raised: %s", e)
                healthy = False
            self._tick(healthy=healthy)

    def _tick(self, healthy: bool) -> None:
        """One probe outcome: update marker + watchdog iff RNS is healthy."""
        with self._lock:
            self._last_rns_healthy = healthy
            self._last_tick = time.time()
        if healthy:
            self._write_heartbeat()
            self._notify("WATCHDOG=1")
        else:
            # RNS wedged (or total freeze): stop pinging so systemd's
            # watchdog fires and restarts the bridge.
            logger.warning(
                "RNS not responsive — stopping liveness pings "
                "(systemd watchdog will act)"
            )

    def _write_heartbeat(self) -> None:
        try:
            tmp = self.heartbeat_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "pid": os.getpid()}, f)
            os.replace(tmp, self.heartbeat_file)
        except OSError as e:
            logger.debug("could not write idle-heartbeat marker: %s", e)

    # ── introspection ────────────────────────────────────────────

    def snapshot(self) -> dict:
        """State for /status: last tick, RNS-healthy flag, marker age."""
        with self._lock:
            last_tick = self._last_tick
            rns_healthy = self._last_rns_healthy
        marker_age = None
        try:
            marker_age = time.time() - os.stat(self.heartbeat_file).st_mtime
        except OSError:
            pass
        return {
                "last_tick": last_tick,
                "last_tick_age_s": (time.time() - last_tick) if last_tick else None,
                "rns_healthy": rns_healthy,
                "marker_age_s": marker_age,
            }
