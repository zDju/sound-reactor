"""Spike helpers: find the WoW X11 window on XWayland, check focus, grab its picture."""

from __future__ import annotations

import numpy as np
from Xlib import X, display

WOW_TITLE = "World of Warcraft"


def open_display(name: str = ":1") -> display.Display:
    return display.Display(name)


def find_window(d: display.Display, title: str = WOW_TITLE):
    root = d.screen().root
    ids = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType).value
    for wid in ids:
        w = d.create_resource_object("window", wid)
        if w.get_wm_name() == title:
            return w
    return None


def active_window_id(d: display.Display) -> int:
    root = d.screen().root
    prop = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
    return int(prop.value[0]) if prop and len(prop.value) else 0


def input_state(d: display.Display) -> dict[str, list[int]]:
    """Read X11's held keys/buttons; this cannot detect stale state inside the game."""
    keymap = d.query_keymap()
    pointer = d.screen().root.query_pointer()
    return {
        "keycodes": [code for code in range(256) if keymap[code // 8] & (1 << (code % 8))],
        "buttons": [
            number for number, mask in enumerate(
                (X.Button1Mask, X.Button2Mask, X.Button3Mask, X.Button4Mask, X.Button5Mask), 1
            ) if pointer.mask & mask
        ],
    }


def grab(w) -> np.ndarray:
    g = w.get_geometry()
    raw = w.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xFFFFFFFF).data
    img = np.frombuffer(raw, dtype=np.uint8).reshape(g.height, g.width, 4)
    return img[:, :, [2, 1, 0]]  # BGRX -> RGB


def save_png(img: np.ndarray, path: str, scale: int = 4) -> None:
    from PIL import Image

    Image.fromarray(np.ascontiguousarray(img[::scale, ::scale])).save(path)


def send_key(d: display.Display, w, key: str, hold: float = 0.06) -> None:
    """Press and release ``key`` (an X keysym name such as "9" or "f") in window ``w`` with
    XSendEvent. The event can only reach that window; the game acts on it only when focused."""
    import time

    from Xlib import XK
    from Xlib.protocol import event

    keycode = d.keysym_to_keycode(XK.string_to_keysym(key))
    if not keycode:
        raise ValueError(f"no keycode for {key!r}")
    root = d.screen().root
    def make_event(kind):
        return kind(
            time=X.CurrentTime, root=root, window=w, same_screen=1, child=X.NONE,
            root_x=0, root_y=0, event_x=0, event_y=0, state=0, detail=keycode,
        )  # fmt: skip
    try:
        w.send_event(make_event(event.KeyPress), propagate=True)
        d.sync()
        time.sleep(hold)
    finally:
        # Attempt the release even if the hold is interrupted, including by Ctrl+C.
        w.send_event(make_event(event.KeyRelease), propagate=True)
        d.sync()
