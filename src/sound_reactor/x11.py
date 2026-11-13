"""X11 (XWayland) access: windows, focus, held keys/buttons, and keys sent with XSendEvent.

XSendEvent delivers a key event to one window and nowhere else; the game acts on it only while
it has focus (measured in phase 1, see PLAN.md). Native Wayland windows cannot be targeted.
Held keys and buttons are read from the X server, which cannot reveal stale state inside the
target application.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from Xlib import XK, X, display
from Xlib.error import XError
from Xlib.protocol import event

from .keys import describe_held

KEY_HOLD = 0.06  # s between KeyPress and KeyRelease
BUTTON_MASKS = (X.Button1Mask, X.Button2Mask, X.Button3Mask, X.Button4Mask, X.Button5Mask)


def keysym(name: str) -> int:
    """The X keysym for a key name such as ``9``, ``f``, ``F1``, ``space`` or ``Return``."""
    sym = XK.string_to_keysym(name)
    if not sym:
        raise ValueError(f"unknown key {name!r}: use X key names such as 9, f, F1, space, Return")
    return sym


_NAMES = {getattr(XK, n): n[3:] for n in dir(XK) if n.startswith("XK_")}


def key_name(sym: int) -> str | None:
    """The X name of a keysym (``0x21`` -> ``exclam``), if known."""
    return _NAMES.get(sym)


def _folded(sym: int) -> int | str:
    """Latin-1 keysyms compare without case (the F key gives ``f`` unshifted, ``F`` shifted)."""
    return chr(sym).casefold() if 0 < sym < 0x100 else sym


def open_display(name: str | None = None) -> display.Display:
    """A connection to the X server (``$DISPLAY`` by default; XWayland under Wayland)."""
    return display.Display(name)


def window_title(d: display.Display, w: Any) -> str:
    with contextlib.suppress(XError):
        prop = w.get_full_property(d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"))
        if prop is not None and prop.value:
            value = prop.value
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    with contextlib.suppress(XError):
        name = w.get_wm_name()
        if name:
            return name.decode("latin-1") if isinstance(name, bytes) else str(name)
    return ""


def _client_windows(d: display.Display) -> list[Any]:
    root = d.screen().root
    prop = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
    ids = prop.value if prop is not None else []
    return [d.create_resource_object("window", int(wid)) for wid in ids]


def list_windows(d: display.Display) -> list[str]:
    """Titles of the X11 client windows (as listed by the window manager)."""
    titles = []
    for w in _client_windows(d):
        title = window_title(d, w)
        if title and title not in titles:
            titles.append(title)
    return titles


def find_window(d: display.Display, title: str) -> Any | None:
    """The first client window whose title (``_NET_WM_NAME`` or ``WM_NAME``) is ``title``."""
    for w in _client_windows(d):
        if window_title(d, w) == title:
            return w
        with contextlib.suppress(XError):
            if w.get_wm_name() == title:  # what spikes/xwin.py matched
                return w
    return None


def active_window_id(d: display.Display) -> int:
    root = d.screen().root
    prop = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
    return int(prop.value[0]) if prop is not None and len(prop.value) else 0


def held_input(d: display.Display) -> tuple[list[int], list[int]]:
    """Keycodes and mouse button numbers the X server reports as held down."""
    keymap = d.query_keymap()
    pointer = d.screen().root.query_pointer()
    keycodes = [code for code in range(256) if keymap[code // 8] & (1 << (code % 8))]
    buttons = [n for n, mask in enumerate(BUTTON_MASKS, 1) if pointer.mask & mask]
    return keycodes, buttons


def send_key(d: display.Display, w: Any, keycode: int, hold: float = KEY_HOLD) -> None:
    """Press and release ``keycode`` in window ``w`` with XSendEvent; the release is attempted
    even if the hold is interrupted."""
    root = d.screen().root

    def make(kind: Any) -> Any:
        return kind(
            time=X.CurrentTime, root=root, window=w, same_screen=1, child=X.NONE,
            root_x=0, root_y=0, event_x=0, event_y=0, state=0, detail=keycode,
        )  # fmt: skip

    try:
        w.send_event(make(event.KeyPress), propagate=True)
        d.sync()
        time.sleep(hold)
    finally:
        w.send_event(make(event.KeyRelease), propagate=True)
        d.sync()


class Target:
    """The window that receives keys: focus check, held-input check and key presses.

    Use it from one thread only (python-xlib connections are not thread-safe).
    """

    def __init__(self, d: display.Display, window: Any, title: str) -> None:
        self.display = d
        self.window = window
        self.title = title

    @classmethod
    def find(cls, title: str, display_name: str | None = None) -> Target:
        d = open_display(display_name)
        w = find_window(d, title)
        if w is None:
            d.close()
            raise LookupError(f"no X11 window titled {title!r} is open")
        return cls(d, w, title)

    def focused(self) -> bool:
        return active_window_id(self.display) == self.window.id

    def held(self) -> str | None:
        keycodes, buttons = held_input(self.display)
        names = [key_name(self.display.keycode_to_keysym(c, 0)) or f"keycode {c}" for c in keycodes]
        return describe_held(names, buttons)

    def keycode(self, key: str) -> int:
        """The keycode that gives ``key`` without modifiers (``F`` and ``f`` are the same key).

        Keys are sent without modifier state, so a name that needs Shift or AltGr on the current
        layout (``exclam``, ``plus``) would silently send the key's unshifted symbol: refused.
        """
        sym = keysym(key)
        codes = [code for code, _ in self.display.keysym_to_keycodes(sym)]
        for code in codes:
            if _folded(self.display.keycode_to_keysym(code, 0)) == _folded(sym):
                return code
        if not codes:
            raise ValueError(f"key {key!r} is not on the current keyboard layout")
        plain = key_name(self.display.keycode_to_keysym(codes[0], 0)) or "?"
        raise ValueError(
            f"key {key!r} needs a modifier on this layout (the key gives {plain!r} without one) "
            "and modifiers are not sent; bind the action to a key without modifiers"
        )

    def describe_key(self, key: str) -> str:
        return f"keycode {self.keycode(key)}"

    def press(self, key: str) -> None:
        send_key(self.display, self.window, self.keycode(key))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.display.close()


def window_titles() -> list[str]:
    """Titles of open X11 windows, for pickers; empty if there is no X server."""
    try:
        d = open_display()
    except Exception:
        return []
    try:
        return list_windows(d)
    finally:
        d.close()
