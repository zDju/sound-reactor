"""Test isolation: nothing here may reach the user's display, profiles, runs or sound library,
and no test may ever send a real key, on any platform."""

from __future__ import annotations

import importlib.util
import os

# Before anything imports Qt or Xlib: no window may appear and no X connection may be made.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("DISPLAY", None)
os.environ.pop("WAYLAND_DISPLAY", None)

import pytest


@pytest.fixture(autouse=True)
def isolated_homes(tmp_path, monkeypatch):
    """Profiles, run folders and the detector library live in a temporary folder (the XDG
    variables on Linux, APPDATA and LOCALAPPDATA on Windows)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setenv("SOUND_EVENT_DETECTOR_HOME", str(tmp_path / "library"))
    return tmp_path


@pytest.fixture(autouse=True)
def no_desktop(monkeypatch):
    """No test may reach the X server, the session bus or the Windows desktop (tests that need
    them use fakes; the Windows-only tests make a ``win32.User32`` themselves), and the real
    SendInput is never called."""

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach the desktop")

    def refuse_keys(*args, **kwargs):
        raise AssertionError("a test tried to send a real key")

    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    if importlib.util.find_spec("Xlib") is not None:  # Linux
        monkeypatch.setattr("sound_reactor.x11.open_display", refuse)
    if importlib.util.find_spec("jeepney") is not None:
        monkeypatch.setattr("sound_reactor.screensaver.open_dbus_connection", refuse)
    monkeypatch.setattr("sound_reactor.win32.default_api", refuse)
    monkeypatch.setattr("sound_reactor.win32.User32.send_input", refuse_keys)
