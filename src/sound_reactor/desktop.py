"""The desktop of this platform: X11 (also XWayland) on Linux, Win32 on Windows.

Each backend finds the target window by its title, checks focus and held keys, presses keys and
keeps the screen from locking, behind the same interfaces (:class:`~.engine.Keyboard`,
:class:`~.engine.Inhibitor`, a focus check). :func:`current` picks the backend by
``sys.platform``; nothing platform-specific is imported before it is called.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .engine import Inhibitor


class Target(Protocol):
    """The window that receives keys (an engine :class:`~.engine.Keyboard` plus its checks)."""

    title: str

    def focused(self) -> bool: ...

    def held(self) -> str | None: ...

    def press(self, key: str) -> None: ...

    def keycode(self, key: str) -> int:
        """The code ``key`` is sent as; ValueError if it cannot be sent without a modifier."""
        ...

    def describe_key(self, key: str) -> str:
        """``keycode 41`` / ``scan code 0x21``; ValueError as :meth:`keycode`."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Backend:
    name: str  # "X11" or "Windows"
    sends_with: str  # how keys are sent, for messages
    reach: str  # what find_target talks to, for messages ("the X server")
    window_titles: Callable[[], list[str]]  # open windows, for pickers ([] if unreachable)
    find_target: Callable[[str], Target]  # LookupError: no such window
    key_error: Callable[[str], str | None]  # why a key name is unknown here (None: known)
    target_problems: Callable[[Target], list[str]]  # what would make keys vanish (UIPI)
    target_about: Callable[[Target], list[str]]  # more about the window, for the doctor
    window_problems: Callable[[str], list[str]]  # the same, by window title (for the window)
    app_windows: Callable[[int], list[str]]  # titles of a process's windows (if known)
    inhibitor: Callable[[], Inhibitor]
    about: Callable[[], list[str]]  # lines for `sound-reactor doctor`


def current() -> Backend:
    if sys.platform == "win32":
        from . import win32

        return win32.backend()
    return x11_backend()


def x11_backend() -> Backend:
    """X11 through python-xlib, the screen lock through the session bus (jeepney)."""
    from . import screensaver, x11

    def key_error(name: str) -> str | None:
        try:
            x11.keysym(name)
        except ValueError as e:
            return str(e)
        return None

    def about() -> list[str]:
        session = os.environ.get("XDG_SESSION_TYPE") or "?"
        through = " (X11 windows through XWayland)" if session == "wayland" else ""
        return [f"DISPLAY {os.environ.get('DISPLAY') or 'not set'}, session {session}{through}"]

    return Backend(
        name="X11",
        sends_with="XSendEvent to the window",
        reach="the X server",
        window_titles=lambda: x11.window_titles(),
        find_target=lambda title: x11.Target.find(title),
        key_error=key_error,
        target_problems=lambda target: [],
        target_about=lambda target: [],
        window_problems=lambda title: [],
        app_windows=lambda pid: [],
        inhibitor=lambda: screensaver.ScreenSaverInhibitor(),
        about=about,
    )
