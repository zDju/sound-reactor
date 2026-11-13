"""The screen-lock inhibition with a fake D-Bus connection (never the real session bus)."""

from __future__ import annotations

import pytest

from sound_reactor import screensaver


class FakeConnection:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False
        self.fail_on = fail_on

    def send_and_get_reply(self, message, timeout):
        member = message.header.fields[3]  # HeaderFields.member
        self.calls.append((member, message.body))
        assert timeout == screensaver.TIMEOUT
        if member == self.fail_on:
            raise TimeoutError("no reply")
        return member

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def bus(monkeypatch):
    connections: list[FakeConnection] = []

    def connect(fail_on: str | None = None):
        def open_connection(bus: str) -> FakeConnection:
            assert bus == "SESSION"
            connections.append(FakeConnection(fail_on))
            return connections[-1]

        monkeypatch.setattr(screensaver, "open_dbus_connection", open_connection)
        return connections

    monkeypatch.setattr(
        screensaver, "unwrap_msg", lambda reply: (42,) if reply == "Inhibit" else ()
    )
    return connect


def test_inhibit_and_release(bus):
    connections = bus()
    inhibitor = screensaver.ScreenSaverInhibitor()
    assert "cookie 42" in inhibitor.inhibit("fishing")
    assert inhibitor.inhibit("again") == "already held"
    (conn,) = connections
    assert not conn.closed  # the inhibition lives as long as the connection
    inhibitor.release()
    assert conn.calls == [("Inhibit", ("sound-reactor", "fishing")), ("UnInhibit", (42,))]
    assert conn.closed
    inhibitor.release()  # nothing held: no call
    assert len(conn.calls) == 2


def test_failed_inhibit_closes_the_connection(bus):
    connections = bus(fail_on="Inhibit")
    inhibitor = screensaver.ScreenSaverInhibitor()
    with pytest.raises(TimeoutError):
        inhibitor.inhibit("fishing")
    assert connections[0].closed
    inhibitor.release()  # nothing held
    assert connections[0].calls == [("Inhibit", ("sound-reactor", "fishing"))]


def test_failed_release_still_closes_the_connection(bus):
    connections = bus(fail_on="UnInhibit")
    inhibitor = screensaver.ScreenSaverInhibitor()
    inhibitor.inhibit("fishing")
    with pytest.raises(TimeoutError):
        inhibitor.release()
    assert connections[0].closed
    inhibitor.release()  # already let go
    assert len(connections[0].calls) == 2
