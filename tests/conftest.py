"""Test isolation: nothing here may reach the user's display, profiles, runs or sound library."""

from __future__ import annotations

import os

# Before anything imports Qt or Xlib: no window may appear and no X connection may be made.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("DISPLAY", None)
os.environ.pop("WAYLAND_DISPLAY", None)

import pytest


@pytest.fixture(autouse=True)
def isolated_homes(tmp_path, monkeypatch):
    """Profiles, run folders and the detector library live in a temporary folder."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SOUND_EVENT_DETECTOR_HOME", str(tmp_path / "library"))
    return tmp_path


@pytest.fixture(autouse=True)
def no_desktop(monkeypatch):
    """No test may reach the X server or the session bus (tests that need them use fakes)."""

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach the desktop")

    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr("sound_reactor.x11.open_display", refuse)
    monkeypatch.setattr("sound_reactor.screensaver.open_dbus_connection", refuse)
