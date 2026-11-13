"""Keeping the screen from locking while a live run is active.

Keys sent with XSendEvent do not count as user activity, so after a few idle minutes KDE's
screen locker takes focus from the game, which stops the run. A live run therefore holds an
``org.freedesktop.ScreenSaver.Inhibit`` on the session bus (through ``jeepney``, pure Python)
and releases it when it stops. The inhibition belongs to this D-Bus connection: if the process
dies, the bus drops the connection and the desktop releases it too.
"""

from __future__ import annotations

import contextlib

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import DBusConnection, open_dbus_connection
from jeepney.wrappers import unwrap_msg

APP_NAME = "sound-reactor"
SCREENSAVER = DBusAddress(
    "/org/freedesktop/ScreenSaver",
    bus_name="org.freedesktop.ScreenSaver",
    interface="org.freedesktop.ScreenSaver",
)
TIMEOUT = 5.0  # s for each D-Bus call


class ScreenSaverInhibitor:
    """Holds the inhibition between ``inhibit()`` and ``release()`` (an engine ``Inhibitor``)."""

    def __init__(self) -> None:
        self._conn: DBusConnection | None = None
        self._cookie: int | None = None

    def inhibit(self, reason: str) -> str:
        if self._conn is not None:
            return "already held"
        conn = open_dbus_connection(bus="SESSION")
        try:
            call = new_method_call(SCREENSAVER, "Inhibit", "ss", (APP_NAME, reason))
            (cookie,) = unwrap_msg(conn.send_and_get_reply(call, timeout=TIMEOUT))
        except BaseException:
            conn.close()
            raise
        self._conn, self._cookie = conn, int(cookie)
        return f"org.freedesktop.ScreenSaver.Inhibit, cookie {self._cookie}"

    def release(self) -> None:
        conn, cookie = self._conn, self._cookie
        self._conn = self._cookie = None
        if conn is None:
            return
        try:
            call = new_method_call(SCREENSAVER, "UnInhibit", "u", (cookie,))
            unwrap_msg(conn.send_and_get_reply(call, timeout=TIMEOUT))
        finally:
            with contextlib.suppress(Exception):
                conn.close()  # closing the connection releases it as well
