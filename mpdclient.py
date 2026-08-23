"""Thin wrapper around python-mpd2's MPDClient -- adds auto-reconnect
(per the brief: "bebop should automatically reconnect if MPD is
temporarily unavailable"), so every other bebop module just calls
Client.call(...) without thinking about connection state itself.

bebop is a graphical MPD *client* only -- no audio decoding, library
indexing, or playback-queue logic lives here, all of that is MPD's job
(see the brief's Software Architecture section).
"""
import logging

from mpd import ConnectionError as MPDConnectionError
from mpd import MPDClient

log = logging.getLogger("bebop.mpdclient")

# Connection-shaped failures worth reconnecting over. CommandError/
# ProtocolError are excluded on purpose -- those mean MPD is alive and
# rejected something (a bad argument, wrong player state), not that the
# connection died, so retrying after a fresh connect would just repeat
# the same rejection.
RECONNECT_ERRORS = (MPDConnectionError, OSError, BrokenPipeError, ConnectionResetError)


class Client:
    def __init__(self, host, port, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._client = MPDClient()
        self._client.timeout = timeout
        self._connected = False

    @property
    def connected(self):
        return self._connected

    def _connect(self):
        try:
            self._client.connect(self.host, self.port)
            self._connected = True
        except RECONNECT_ERRORS as exc:
            self._connected = False
            log.warning("MPD connect to %s:%s failed: %s", self.host, self.port, exc)
            raise

    def call(self, method, *args, **kwargs):
        """Calls method on the underlying MPDClient (e.g.
        call("status"), call("play", 3)), reconnecting once and
        retrying if the connection looks dead. Raises whatever the
        underlying call raises if the retry also fails, or if it fails
        for a non-connection reason (a real command error) -- callers
        decide what "MPD unavailable" should look like on screen,
        this only handles the reconnect mechanics."""
        if not self._connected:
            self._connect()
        try:
            return getattr(self._client, method)(*args, **kwargs)
        except RECONNECT_ERRORS as exc:
            log.warning("MPD call %r failed (%s), reconnecting", method, exc)
            self._connected = False
            try:
                self._client.disconnect()
            except Exception:  # noqa: BLE001 -- best-effort cleanup of an already-broken connection
                pass
            self._connect()
            return getattr(self._client, method)(*args, **kwargs)
