"""Only the parts that need no X server: key names and held-input descriptions."""

import pytest

from sound_reactor import x11


def test_key_names():
    assert x11.keysym("9") == 0x39
    assert x11.keysym("f") == 0x66
    assert x11.keysym("F1") and x11.keysym("space") and x11.keysym("Return")
    with pytest.raises(ValueError, match="unknown key"):
        x11.keysym("notakey")


def test_describe_held():
    assert x11.describe_held([], []) is None
    assert x11.describe_held(["w"], []) == "key(s) w"
    assert x11.describe_held(["w", "Shift_L"], [1, 3]) == (
        "key(s) w, Shift_L and mouse button(s) 1, 3"
    )


# -- sending keys and the keymap, with a fake display ------------------------------------------

from Xlib import X  # noqa: E402
from Xlib.protocol import event  # noqa: E402

# keycode -> keysym names at shift levels 0 and 1 (a US-like layout)
KEYMAP = {
    10: ("1", "exclam"),
    18: ("9", "parenleft"),
    21: ("equal", "plus"),
    25: ("w", "W"),
    41: ("f", "F"),
    59: ("comma", "less"),
    94: ("less", "greater"),
}


class FakeDisplay:
    def __init__(self, *, fail_sync_at: int | None = None, held=(), buttons=0):
        self.log: list[str] = []
        self.syncs = 0
        self.fail_sync_at = fail_sync_at
        self.held_codes = held
        self.buttons = buttons

    def screen(self):
        display = self

        class Root(int):  # Xlib packs window ids as integers
            def query_pointer(self):
                class Pointer:
                    mask = display.buttons

                return Pointer()

        class Screen:
            root = Root(1)

        return Screen()

    def sync(self):
        self.syncs += 1
        self.log.append("sync")
        if self.syncs == self.fail_sync_at:
            raise ConnectionError("sync failed")

    def keysym_to_keycodes(self, sym):
        found = [
            (code, index)
            for code, names in KEYMAP.items()
            for index, name in enumerate(names)
            if x11.keysym(name) == sym
        ]
        return sorted(found, key=lambda ci: (ci[1], ci[0]))

    def keycode_to_keysym(self, code, index):
        names = KEYMAP.get(code, ())
        return x11.keysym(names[index]) if index < len(names) else 0

    def query_keymap(self):
        keymap = [0] * 32
        for code in self.held_codes:
            keymap[code // 8] |= 1 << (code % 8)
        return keymap


class FakeWindow(int):
    def __new__(cls, display: FakeDisplay, *, fail_press: bool = False):
        w = super().__new__(cls, 77)
        w.display = display
        w.fail_press = fail_press
        w.events = []
        return w

    @property
    def id(self) -> int:
        return int(self)

    def send_event(self, ev, propagate):
        kind = "press" if isinstance(ev, event.KeyPress) else "release"
        self.display.log.append(kind)
        self.events.append((kind, ev.detail, ev.state))
        if kind == "press" and self.fail_press:
            raise ConnectionError("send failed")


def test_send_key_presses_then_releases(monkeypatch):
    monkeypatch.setattr(x11.time, "sleep", lambda s: None)
    d = FakeDisplay()
    w = FakeWindow(d)
    x11.send_key(d, w, 41)
    assert d.log == ["press", "sync", "release", "sync"]
    assert w.events == [("press", 41, 0), ("release", 41, 0)]


def test_release_is_sent_after_ctrl_c_during_the_hold(monkeypatch):
    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(x11.time, "sleep", interrupted)
    d = FakeDisplay()
    with pytest.raises(KeyboardInterrupt):
        x11.send_key(d, FakeWindow(d), 41)
    assert d.log == ["press", "sync", "release", "sync"]


def test_release_is_sent_after_a_failed_sync_or_press(monkeypatch):
    monkeypatch.setattr(x11.time, "sleep", lambda s: None)
    d = FakeDisplay(fail_sync_at=1)
    with pytest.raises(ConnectionError, match="sync failed"):
        x11.send_key(d, FakeWindow(d), 41)
    assert d.log == ["press", "sync", "release", "sync"]
    d = FakeDisplay()
    with pytest.raises(ConnectionError, match="send failed"):
        x11.send_key(d, FakeWindow(d, fail_press=True), 41)
    assert d.log == ["press", "release", "sync"]


def test_keys_that_need_a_modifier_are_refused():
    target = x11.Target(FakeDisplay(), FakeWindow(FakeDisplay()), "Game")
    assert target.keycode("9") == 18
    assert target.keycode("f") == 41 and target.keycode("F") == 41  # the same key
    assert target.keycode("less") == 94  # unshifted on 94, shifted on 59
    for key, plain in (("exclam", "1"), ("plus", "equal"), ("parenleft", "9")):
        with pytest.raises(ValueError, match=f"needs a modifier.*'{plain}'"):
            target.keycode(key)
    with pytest.raises(ValueError, match="not on the current keyboard layout"):
        target.keycode("Return")


def test_press_sends_the_unshifted_keycode(monkeypatch):
    monkeypatch.setattr(x11.time, "sleep", lambda s: None)
    d = FakeDisplay()
    w = FakeWindow(d)
    x11.Target(d, w, "Game").press("F")
    assert w.events == [("press", 41, 0), ("release", 41, 0)]


def test_held_names_keys_and_buttons():
    assert x11.Target(FakeDisplay(), None, "Game").held() is None
    d = FakeDisplay(held=(25, 10), buttons=X.Button1Mask)
    assert x11.Target(d, None, "Game").held() == "key(s) 1, w and mouse button(s) 1"
