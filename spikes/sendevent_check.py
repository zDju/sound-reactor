"""Confirm that synthetic X key events reach the game while another window has focus.

Sends B (bags) four times with XSendEvent only; nothing can leave the game window.

    uv run --with pillow python spikes/sendevent_check.py OUTDIR
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from Xlib import XK, X
from Xlib.protocol import event

sys.path.insert(0, str(Path(__file__).parent))
import xwin  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
d = xwin.open_display()
win = xwin.find_window(d)
root = d.screen().root
keycode = d.keysym_to_keycode(XK.string_to_keysym("b"))


def bag_area(img: np.ndarray) -> np.ndarray:
    h, w, _ = img.shape
    return img[int(h * 0.55) : int(h * 0.95), int(w * 0.75) :].astype(int)


for i in range(4):
    before = xwin.grab(win)
    active = xwin.active_window_id(d)
    for kind in (event.KeyPress, event.KeyRelease):
        ev = kind(
            time=X.CurrentTime, root=root, window=win, same_screen=1, child=X.NONE,
            root_x=0, root_y=0, event_x=0, event_y=0, state=0, detail=keycode,
        )  # fmt: skip
        win.send_event(ev, propagate=True)
        d.flush()
        time.sleep(0.06)
    time.sleep(1.5)
    after = xwin.grab(win)
    xwin.save_png(after, str(out / f"press{i + 1}.png"))
    change = float(np.abs(bag_area(after) - bag_area(before)).mean())
    print(f"press {i + 1}: active {hex(active)} (game {hex(win.id)}), bag-area change {change:5.1f}")
