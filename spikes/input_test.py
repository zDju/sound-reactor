"""Phase 1 spike: which ways of pressing a key does the game accept, focused and unfocused?

Presses B (open/close bags) twice per method and grabs the window before and after.
Safety: keys are sent only after focus has been seen *moving to* the game window, and every
press re-checks that the game still has focus. uinput (a real virtual keyboard, which goes to
whatever has focus) is used only while the game is focused. The unfocused phase uses only
XSendEvent and XTEST, which stay inside XWayland and cannot reach Wayland windows.

    uv run --with pillow python spikes/input_test.py OUTDIR [METHODS] [--no-unfocused]

METHODS is a comma-separated subset of xtest,uinput,sendevent (default: all). XTEST makes
KDE Plasma ask for permission (kwin_eis_prompter) the first time it is used.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from evdev import UInput
from evdev import ecodes as e
from Xlib import XK, X
from Xlib.ext import xtest
from Xlib.protocol import event

sys.path.insert(0, str(Path(__file__).parent))
import xwin  # noqa: E402

OUT = Path(sys.argv[1])
METHODS = sys.argv[2].split(",") if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else [
    "xtest", "uinput", "sendevent",
]  # fmt: skip
UNFOCUSED = "--no-unfocused" not in sys.argv
OUT.mkdir(parents=True, exist_ok=True)
T0 = time.monotonic()


def log(msg: str) -> None:
    line = f"[{time.monotonic() - T0:6.1f}s] {msg}"
    print(line, flush=True)
    with (OUT / "log.txt").open("a") as f:
        f.write(line + "\n")


d = xwin.open_display()
win = xwin.find_window(d)
if win is None:
    log("game window not found")
    sys.exit(1)
keycode = d.keysym_to_keycode(XK.string_to_keysym("b"))
ui = UInput({e.EV_KEY: [e.KEY_B]}, name="sound-reactor spike")  # created early: the
log(f"window {hex(win.id)}, X keycode for b = {keycode}, uinput device ready")  # compositor needs a moment


def focused() -> bool:
    return xwin.active_window_id(d) == win.id


def snap(name: str) -> np.ndarray:
    img = xwin.grab(win)
    xwin.save_png(img, str(OUT / f"{name}.png"))
    return img


def changed(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute change in the bottom-right corner, where the bags open (0-255)."""
    h, w, _ = a.shape
    ra, rb = a[int(h * 0.55) : int(h * 0.95), int(w * 0.75) :], b[int(h * 0.55) : int(h * 0.95), int(w * 0.75) :]
    return float(np.abs(ra.astype(int) - rb.astype(int)).mean())


def press_xtest() -> None:
    xtest.fake_input(d, X.KeyPress, keycode)
    d.sync()
    time.sleep(0.06)
    xtest.fake_input(d, X.KeyRelease, keycode)
    d.sync()


def press_uinput() -> None:
    ui.write(e.EV_KEY, e.KEY_B, 1)
    ui.syn()
    time.sleep(0.06)
    ui.write(e.EV_KEY, e.KEY_B, 0)
    ui.syn()


def press_sendevent() -> None:
    root = d.screen().root
    for kind in (event.KeyPress, event.KeyRelease):
        ev = kind(
            time=X.CurrentTime, root=root, window=win, same_screen=1, child=X.NONE,
            root_x=0, root_y=0, event_x=0, event_y=0, state=0, detail=keycode,
        )  # fmt: skip
        win.send_event(ev, propagate=True)
        d.flush()
        time.sleep(0.06)


def trial(label: str, press, need_focus: bool) -> None:
    for step in ("open", "close"):
        if need_focus and not focused():
            log(f"{label}: game lost focus, skipping")
            return
        before = snap(f"{label}-{step}-before")
        active = xwin.active_window_id(d)
        press()
        time.sleep(1.5)
        after = snap(f"{label}-{step}-after")
        log(f"{label} {step}: active {hex(active)}, bag-area change {changed(before, after):5.1f}")


# 1. wait until focus moves to the game (the user reads the instructions elsewhere first)
log(f"active window now: {hex(xwin.active_window_id(d))} (game: {focused()})")
seen_other = not focused()
deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    if not focused():
        seen_other = True
    elif seen_other:
        break
    time.sleep(0.1)
else:
    log("never saw focus move to the game; nothing was sent")
    sys.exit(0)
log("focus moved to the game; starting in 1 s")
time.sleep(1.0)

# 2. focused: every method
presses = {"xtest": press_xtest, "uinput": press_uinput, "sendevent": press_sendevent}
for name in METHODS:
    trial(f"focused-{name}", presses[name], True)
if not UNFOCUSED:
    ui.close()
    log("done")
    sys.exit(0)

# 3. unfocused: wait for the user to leave the game, then only XWayland-internal methods
log("waiting for focus to leave the game (up to 120 s)")
deadline = time.monotonic() + 120
while focused() and time.monotonic() < deadline:
    time.sleep(0.1)
if focused():
    log("focus never left the game; skipping the unfocused tests")
else:
    log(f"focus left: active = {hex(xwin.active_window_id(d))}; unfocused tests in 1 s")
    time.sleep(1.0)
    trial("unfocused-sendevent", press_sendevent, False)
    trial("unfocused-xtest", press_xtest, False)
ui.close()
log("done")
