"""A simulated Windows desktop for the Win32 backend (``sound_reactor.win32.Api``): windows and
the foreground window, keyboard layouts, held keys, process elevation and the execution state.
It records every key event and execution-state call instead of making it."""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from sound_reactor import win32

US = 0x04090409  # keyboard layout handles (HKL) as GetKeyboardLayout returns them
GERMAN = 0x04070407

SHIFT, ALTGR = 0x100, 0x600  # VkKeyScanExW's shift state, in the high byte

# VkKeyScanExW: character -> shift state | virtual key
_US_CHARS = {
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{d: ord(d) for d in "0123456789"},
    "-": 0xBD, "=": 0xBB, ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE, "`": 0xC0,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "!": SHIFT | 0x31, "+": SHIFT | 0xBB,
}  # fmt: skip
_GERMAN_CHARS = {
    **_US_CHARS,
    "-": 0xBD, "/": SHIFT | 0x37, "=": SHIFT | 0x30, "\\": ALTGR | 0xDB, "[": ALTGR | 0x38,
    "]": ALTGR | 0x39, ";": SHIFT | 0xBC, "'": SHIFT | 0xBF, "`": SHIFT | 0xDD, "+": 0xBB,
}  # fmt: skip
CHARS = {US: _US_CHARS, GERMAN: _GERMAN_CHARS}

# MapVirtualKeyExW(MAPVK_VK_TO_VSC_EX): virtual key -> scan code (0xE0.. for extended keys)
_LETTER_SCANS = dict(
    zip(
        "qwertyuiopasdfghjklzxcvbnm",
        [*range(0x10, 0x1A), *range(0x1E, 0x27), *range(0x2C, 0x33)],
        strict=True,
    )
)
SCANS = {
    **{ord(c.upper()): code for c, code in _LETTER_SCANS.items()},
    **{ord(d): 0x02 + int(d) - 1 if d != "0" else 0x0B for d in "0123456789"},
    0x20: 0x39, 0x0D: 0x1C, 0x1B: 0x01, 0x09: 0x0F, 0x08: 0x0E,  # space Return Esc Tab BS
    0x2D: 0xE052, 0x2E: 0xE053, 0x24: 0xE047, 0x23: 0xE04F, 0x21: 0xE049, 0x22: 0xE051,
    0x25: 0xE04B, 0x26: 0xE048, 0x27: 0xE04D, 0x28: 0xE050,  # arrows
    **{0x6F + n: 0x3A + n for n in range(1, 11)}, 0x7A: 0x57, 0x7B: 0x58,  # F1-F12
    0x6B: 0x4E, 0x6D: 0x4A, 0x6A: 0x37, 0x6F: 0xE035,  # numpad + - * /
    0xBD: 0x0C, 0xBB: 0x0D, 0xBC: 0x33, 0xBE: 0x34, 0xBF: 0x35, 0xBA: 0x27, 0xDE: 0x28,
    0xC0: 0x29, 0xDB: 0x1A, 0xDD: 0x1B, 0xDC: 0x2B,
}  # fmt: skip


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    thread: int
    visible: bool = True


@dataclass
class FakeWindows:
    windows: list[Window] = field(
        default_factory=lambda: [
            Window(0x10, "", 4, 40),  # untitled
            Window(0x20, "World of Warcraft", 1234, 77),
            Window(0x30, "Hidden", 5, 50, visible=False),
            Window(0x40, "Konsole", 55, 66),
            Window(0x50, "World of Warcraft", 999, 99),  # behind the first one
        ]
    )
    foreground: int | Callable[[], int] = 0x20
    layouts: dict[int, int] = field(default_factory=lambda: {0: US, 77: US})
    held: set[int] = field(default_factory=set)  # virtual keys and mouse buttons that are down
    elevated: dict[int | None, bool | OSError] = field(
        default_factory=lambda: {None: False, 1234: False}
    )
    send_results: list[int] = field(default_factory=list)  # what SendInput returns, in turn
    on_send: Callable[[tuple[int, int, int, int]], object] | None = None
    sent: list[tuple[int, int, int, int]] = field(default_factory=list)  # type, vk, scan, flags
    execution: list[tuple[int, int]] = field(default_factory=list)  # (thread ident, flags)
    execution_result: int | None = None  # what SetThreadExecutionState returns (None: real)
    error: int = 0
    _states: dict[int, int] = field(default_factory=dict)

    def foreground_window(self) -> int:
        return self.foreground() if callable(self.foreground) else self.foreground

    def top_level_windows(self) -> list[int]:
        return [w.hwnd for w in self.windows]

    def _window(self, hwnd: int) -> Window | None:
        return next((w for w in self.windows if w.hwnd == hwnd), None)

    def is_visible(self, hwnd: int) -> bool:
        w = self._window(hwnd)
        return w is not None and w.visible

    def window_text(self, hwnd: int) -> str:
        w = self._window(hwnd)
        return w.title if w is not None else ""

    def window_thread_process(self, hwnd: int) -> tuple[int, int]:
        w = self._window(hwnd)
        return (w.thread, w.pid) if w is not None else (0, 0)

    def keyboard_layout(self, thread_id: int) -> int:
        return self.layouts.get(thread_id, 0)

    def layout_name(self, layout: int) -> str:
        return {US: "en-US", GERMAN: "de-DE"}.get(layout, "")

    def vk_key_scan(self, char: str, layout: int) -> int:
        assert len(char) == 1
        assert not char.isalpha(), "letters are mapped by their virtual key"
        return CHARS.get(layout, {}).get(char, -1)

    def vk_to_scan(self, vk: int, layout: int) -> int:
        if layout == GERMAN and vk in (0x59, 0x5A):  # QWERTZ: Y and Z change places
            return {0x59: 0x2C, 0x5A: 0x15}[vk]
        return SCANS.get(vk, 0) if layout in CHARS else 0

    def send_input(self, inputs: list[win32.INPUT]) -> int:
        assert all(ctypes.sizeof(i) == ctypes.sizeof(win32.INPUT) for i in inputs)
        for i in inputs:
            event = (i.type, i.ki.wVk, i.ki.wScan, i.ki.dwFlags)
            self.sent.append(event)
            if self.on_send is not None:
                self.on_send(event)
        result = self.send_results.pop(0) if self.send_results else len(inputs)
        self.error = 0 if result == len(inputs) else 5
        return result

    def last_error(self) -> int:
        return self.error

    def key_down(self, vk: int) -> bool:
        return vk in self.held

    def process_elevated(self, pid: int | None) -> bool:
        value = self.elevated.get(pid, False)
        if isinstance(value, OSError):
            raise value
        return value

    def set_execution_state(self, flags: int) -> int:
        thread = threading.get_ident()
        self.execution.append((thread, flags))
        if self.execution_result is not None:
            return self.execution_result
        previous = self._states.get(thread, win32.ES_CONTINUOUS)
        self._states[thread] = flags
        return previous


def keys_sent(fake: FakeWindows) -> list[tuple[str, int, bool]]:
    """``("down" | "up", scan code, extended)`` per event sent as a scan code."""
    events = []
    for kind, vk, scan, flags in fake.sent:
        assert kind == win32.INPUT_KEYBOARD and vk == 0 and flags & win32.KEYEVENTF_SCANCODE
        up = "up" if flags & win32.KEYEVENTF_KEYUP else "down"
        events.append((up, scan, bool(flags & win32.KEYEVENTF_EXTENDEDKEY)))
    return events
