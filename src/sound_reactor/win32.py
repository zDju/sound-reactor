"""Windows: windows, focus, held keys/buttons, keys sent with SendInput, keeping the screen on,
and the UIPI check, through ctypes (no extra dependency). **Unverified on real Windows.**

SendInput puts key events into the input stream of the desktop: they go to whatever window is
in the foreground. So a key is sent only while the target window is the foreground window,
checked by the engine and once more right before each key (:class:`~.engine.KeyNotSent`
otherwise). Keys are sent as scan codes (``KEYEVENTF_SCANCODE``, the extended flag for the
navigation keys), which games read. The scan code comes from the keyboard layout of the target
window's thread, and a key whose character needs a modifier on that layout is refused, as on
Linux. Once the key-down has been attempted, the key-up is always sent, also on errors.

User Interface Privilege Isolation (UIPI): Windows silently drops input from a process into the
windows of a process with a higher integrity level, i.e. into a game that runs as administrator
while sound-reactor does not. :func:`uipi_problem` reports that before a run starts.

The screen is kept on with ``SetThreadExecutionState`` while a live run is active. That state
belongs to the thread that sets it, so :class:`ExecutionStateInhibitor` sets and clears it on a
thread of its own.

Every Windows call goes through an :class:`Api` (:class:`User32` on Windows), so all of this is
tested on any platform against a fake one.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol

from . import keys
from .desktop import Backend
from .engine import FocusLostDuringKey, KeyNotSent

KEY_HOLD = 0.06  # s between key-down and key-up

# SendInput and MapVirtualKeyExW
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC_EX = 4

# SetThreadExecutionState
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_CONTINUOUS = 0x80000000
AWAKE = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

# console control events (SetConsoleCtrlHandler)
CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 2, 5, 6
CLOSE_REASONS = {
    CTRL_CLOSE_EVENT: "the console window was closed",
    CTRL_LOGOFF_EVENT: "Windows is logging off",
    CTRL_SHUTDOWN_EVENT: "Windows is shutting down",
}
CLOSE_WAIT = 4.0  # s to let a run finish: Windows ends the process about 5 s after the event

# processes and tokens
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TokenElevation

# Virtual keys of the portable names that type no character (the others come from the layout)
VK_OF = {
    "space": 0x20, "Return": 0x0D, "Escape": 0x1B, "Tab": 0x09, "BackSpace": 0x08,
    "Delete": 0x2E, "Insert": 0x2D, "Home": 0x24, "End": 0x23, "Page_Up": 0x21, "Prior": 0x21,
    "Page_Down": 0x22, "Next": 0x22, "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
    **{f"F{n}": 0x6F + n for n in range(1, 13)},
    "KP_Add": 0x6B, "KP_Subtract": 0x6D, "KP_Multiply": 0x6A, "KP_Divide": 0x6F,
    "KP_Enter": 0x0D,
}  # fmt: skip
# The navigation keys share their scan codes with the keypad: they are "extended" keys, and so
# are the keypad's Enter and / (Enter is VK_RETURN like the main one; only the flag differs).
EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x6F})
EXTENDED_NAMES = frozenset({"KP_Enter", "KP_Divide"})
# Mouse buttons, numbered as X11 does: left 1, middle 2, right 3, side buttons 8 and 9.
MOUSE_BUTTONS = {0x01: 1, 0x04: 2, 0x02: 3, 0x05: 8, 0x06: 9}
# Shift, Ctrl and Alt are also reported through their left and right keys, named below.
GENERIC_MODIFIERS = frozenset({0x10, 0x11, 0x12})
VK_NAMES = {
    0x14: "Caps_Lock", 0x5B: "Super_L", 0x5C: "Super_R", 0x5D: "Menu", 0xA0: "Shift_L",
    0xA1: "Shift_R", 0xA2: "Control_L", 0xA3: "Control_R", 0xA4: "Alt_L", 0xA5: "Alt_R",
}  # fmt: skip
MODIFIER_BITS = ((1, "Shift"), (2, "Ctrl"), (4, "Alt"))  # VkKeyScanExW's shift state


# -- structures (fixed-size fields: the same layout on every platform, as on Windows) ---------

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    """SendInput's INPUT: 40 bytes on 64-bit Windows, 28 on 32-bit (SendInput checks cbSize)."""

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUT_UNION)]


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", ctypes.c_uint32)]


def key_input(scan: int, flags: int) -> INPUT:
    """A keyboard INPUT for scan code ``scan`` (the virtual key is left 0: with
    KEYEVENTF_SCANCODE, Windows ignores it)."""
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


# -- the Windows calls ------------------------------------------------------------------------


class Api(Protocol):
    """The Windows calls this backend makes (:class:`User32` on Windows; fakes in tests)."""

    def foreground_window(self) -> int:
        """GetForegroundWindow (0: none)."""
        ...

    def top_level_windows(self) -> list[int]:
        """EnumWindows: every top-level window, front to back."""
        ...

    def is_visible(self, hwnd: int) -> bool: ...

    def window_text(self, hwnd: int) -> str: ...

    def window_thread_process(self, hwnd: int) -> tuple[int, int]:
        """GetWindowThreadProcessId: (thread id, process id); (0, 0) for no window."""
        ...

    def keyboard_layout(self, thread_id: int) -> int:
        """GetKeyboardLayout of a thread (0: this thread)."""
        ...

    def layout_name(self, layout: int) -> str:
        """The language of a keyboard layout (LCIDToLocaleName of its low word), e.g. de-DE."""
        ...

    def vk_key_scan(self, char: str, layout: int) -> int:
        """VkKeyScanExW: shift state << 8 | virtual key, or -1 if no key types ``char``."""
        ...

    def vk_to_scan(self, vk: int, layout: int) -> int:
        """MapVirtualKeyExW(MAPVK_VK_TO_VSC_EX): the scan code, 0xE0.. for extended keys,
        0 for none."""
        ...

    def send_input(self, inputs: list[INPUT]) -> int:
        """SendInput: how many events were inserted."""
        ...

    def last_error(self) -> int:
        """GetLastError after the previous call on this thread."""
        ...

    def key_down(self, vk: int) -> bool:
        """GetAsyncKeyState: the key or mouse button is down now."""
        ...

    def process_elevated(self, pid: int | None) -> bool:
        """The process (None: this one) runs elevated; OSError if that cannot be read."""
        ...

    def set_execution_state(self, flags: int) -> int:
        """SetThreadExecutionState for the calling thread: the previous state, 0 if it failed."""
        ...


class User32:
    """:class:`Api` through ctypes, on Windows."""

    def __init__(self) -> None:
        from ctypes import wintypes as w

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._wintypes = w
        self._enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        def fn(dll: ctypes.WinDLL, name: str, restype: object, *argtypes: object) -> Callable:
            f = getattr(dll, name)
            f.restype = restype
            f.argtypes = list(argtypes)
            return f

        dword_p = ctypes.POINTER(w.DWORD)
        self._foreground = fn(user32, "GetForegroundWindow", w.HWND)
        self._enum_windows = fn(user32, "EnumWindows", w.BOOL, self._enum_proc, w.LPARAM)
        self._is_visible = fn(user32, "IsWindowVisible", w.BOOL, w.HWND)
        self._text_length = fn(user32, "GetWindowTextLengthW", ctypes.c_int, w.HWND)
        self._text = fn(user32, "GetWindowTextW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        self._thread_process = fn(user32, "GetWindowThreadProcessId", w.DWORD, w.HWND, dword_p)
        self._layout = fn(user32, "GetKeyboardLayout", w.HKL, w.DWORD)
        self._vk_key_scan = fn(user32, "VkKeyScanExW", ctypes.c_short, w.WCHAR, w.HKL)
        self._map_vk = fn(user32, "MapVirtualKeyExW", w.UINT, w.UINT, w.UINT, w.HKL)
        self._send_input = fn(
            user32, "SendInput", w.UINT, w.UINT, ctypes.POINTER(INPUT), ctypes.c_int
        )
        self._async_key_state = fn(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
        self._current_process = fn(kernel32, "GetCurrentProcess", w.HANDLE)
        self._open_process = fn(kernel32, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        self._locale_name = fn(
            kernel32, "LCIDToLocaleName", ctypes.c_int, w.DWORD, w.LPWSTR, ctypes.c_int, w.DWORD
        )
        self._close = fn(kernel32, "CloseHandle", w.BOOL, w.HANDLE)
        self._execution_state = fn(
            kernel32, "SetThreadExecutionState", ctypes.c_uint32, ctypes.c_uint32
        )
        self._open_token = fn(
            advapi32, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)
        )
        self._token_information = fn(
            advapi32, "GetTokenInformation", w.BOOL, w.HANDLE, ctypes.c_int,
            ctypes.POINTER(TOKEN_ELEVATION), w.DWORD, dword_p,
        )  # fmt: skip

    def foreground_window(self) -> int:
        return self._foreground() or 0

    def top_level_windows(self) -> list[int]:
        found: list[int] = []

        def add(hwnd: int | None, _: int) -> bool:
            found.append(hwnd or 0)
            return True

        callback = self._enum_proc(add)  # referenced until EnumWindows returns
        if not self._enum_windows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())  # pyright: ignore
        return found

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._is_visible(hwnd))

    def window_text(self, hwnd: int) -> str:
        length = self._text_length(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        copied = self._text(hwnd, buffer, length + 1)
        return buffer.value[:copied] if copied > 0 else ""

    def window_thread_process(self, hwnd: int) -> tuple[int, int]:
        pid = self._wintypes.DWORD()
        thread = self._thread_process(hwnd, ctypes.byref(pid))
        return int(thread), int(pid.value)

    def keyboard_layout(self, thread_id: int) -> int:
        return self._layout(thread_id) or 0

    def layout_name(self, layout: int) -> str:
        buffer = ctypes.create_unicode_buffer(85)  # LOCALE_NAME_MAX_LENGTH
        length = self._locale_name(layout & 0xFFFF, buffer, len(buffer), 0)
        return buffer.value if length > 0 else ""

    def vk_key_scan(self, char: str, layout: int) -> int:
        return int(self._vk_key_scan(char, layout or None))

    def vk_to_scan(self, vk: int, layout: int) -> int:
        return int(self._map_vk(vk, MAPVK_VK_TO_VSC_EX, layout or None))

    def send_input(self, inputs: list[INPUT]) -> int:
        array = (INPUT * len(inputs))(*inputs)
        return int(self._send_input(len(inputs), array, ctypes.sizeof(INPUT)))

    def last_error(self) -> int:
        return ctypes.get_last_error()  # pyright: ignore

    def key_down(self, vk: int) -> bool:
        return bool(self._async_key_state(vk) & 0x8000)

    def process_elevated(self, pid: int | None) -> bool:
        w = self._wintypes
        if pid is None:
            process, owned = self._current_process(), False
        else:
            process, owned = self._open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid), True
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())  # pyright: ignore
        try:
            token = w.HANDLE()
            if not self._open_token(process, TOKEN_QUERY, ctypes.byref(token)):
                raise ctypes.WinError(ctypes.get_last_error())  # pyright: ignore
            try:
                elevation, size = TOKEN_ELEVATION(), w.DWORD()
                if not self._token_information(
                    token, TOKEN_ELEVATION_CLASS, ctypes.byref(elevation),
                    ctypes.sizeof(elevation), ctypes.byref(size),
                ):  # fmt: skip
                    raise ctypes.WinError(ctypes.get_last_error())  # pyright: ignore
                return bool(elevation.TokenIsElevated)
            finally:
                self._close(token)
        finally:
            if owned:
                self._close(process)

    def set_execution_state(self, flags: int) -> int:
        return int(self._execution_state(flags))


@functools.cache
def _user32() -> User32:
    return User32()


def default_api() -> Api:
    if sys.platform != "win32":
        raise OSError("the Windows desktop backend needs Windows")
    return _user32()


# -- windows ----------------------------------------------------------------------------------


def visible_windows(api: Api) -> list[tuple[int, str]]:
    """Visible top-level windows that have a title, front to back."""
    found = []
    for hwnd in api.top_level_windows():
        if api.is_visible(hwnd):
            title = api.window_text(hwnd)
            if title:
                found.append((hwnd, title))
    return found


def list_windows(api: Api) -> list[str]:
    titles: list[str] = []
    for _, title in visible_windows(api):
        if title not in titles:
            titles.append(title)
    return titles


def find_window(api: Api, title: str) -> int | None:
    """The frontmost visible top-level window titled exactly ``title``."""
    return next((hwnd for hwnd, t in visible_windows(api) if t == title), None)


def app_windows(api: Api, pid: int) -> list[str]:
    """Titles of the visible windows of process ``pid``."""
    return [t for hwnd, t in visible_windows(api) if api.window_thread_process(hwnd)[1] == pid]


def window_titles(api: Api | None = None) -> list[str]:
    """Titles of open windows, for pickers; empty if they cannot be listed."""
    try:
        return list_windows(api or default_api())
    except Exception:
        return []


# -- keys -------------------------------------------------------------------------------------


def key_error(name: str) -> str | None:
    """Why ``name`` is not a key name on Windows (None: it is one)."""
    if keys.is_portable(name):
        return None
    return f"unknown key {name!r}: on Windows use {keys.PORTABLE_TEXT}"


def _modifiers(shift: int) -> str:
    if shift & 0x06 == 0x06 and not shift & 0x01:
        return "AltGr"
    return "+".join(name for bit, name in MODIFIER_BITS if shift & bit) or f"state {shift}"


def scan_code(api: Api, key: str, layout: int) -> tuple[int, bool]:
    """The scan code that presses ``key`` on keyboard ``layout``, and whether it is an extended
    key. ValueError for a name that is not portable, a key that is not on the layout, or one
    whose character needs a modifier there.

    Letters are the keys Windows calls VK_A-VK_Z on every layout (``F`` and ``f`` are the same
    key, as on Linux); digits and punctuation are looked up by their character, so that ``9``
    on a layout where it needs Shift (AZERTY) is refused, as on Linux."""
    vk = VK_OF.get(key)
    if vk is None and len(key) == 1 and key.lower() in keys.LETTERS:
        vk = ord(key.upper())
    if vk is None:
        char = keys.character(key)
        if char is None:
            raise ValueError(key_error(key))
        found = api.vk_key_scan(char, layout)
        if found == -1 or found & 0xFF == 0xFF:
            raise ValueError(f"key {key!r} is not on the current keyboard layout")
        vk, shift = found & 0xFF, (found >> 8) & 0xFF
        if shift:
            raise ValueError(
                f"key {key!r} needs a modifier on this layout ({_modifiers(shift)}) and "
                "modifiers are not sent; bind the action to a key without modifiers"
            )
    code = api.vk_to_scan(vk, layout)
    if not code & 0xFF:
        raise ValueError(f"key {key!r} is not on the current keyboard layout")
    extended = (code >> 8) in (0xE0, 0xE1) or vk in EXTENDED_VKS or key in EXTENDED_NAMES
    return code & 0xFF, extended


def vk_name(vk: int) -> str:
    """A name for a virtual key, for messages (``f``, ``9``, ``F1``, ``Shift_L``...)."""
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    named = next((name for name, code in VK_OF.items() if code == vk), None)
    return named or VK_NAMES.get(vk, f"VK 0x{vk:02X}")


def held_input(api: Api) -> str | None:
    """Keys and mouse buttons that are down now (GetAsyncKeyState), or None."""
    pressed, buttons = [], []
    for vk in range(0x01, 0xFF):
        if vk in GENERIC_MODIFIERS or not api.key_down(vk):
            continue
        if vk in MOUSE_BUTTONS:
            buttons.append(MOUSE_BUTTONS[vk])
        else:
            pressed.append(vk_name(vk))
    return keys.describe_held(pressed, sorted(buttons))


def _send(api: Api, scan: int, flags: int, what: str) -> None:
    sent = api.send_input([key_input(scan, flags)])
    if sent != 1:
        raise OSError(f"SendInput did not insert the key-{what} (Windows error {api.last_error()})")


def send_key(
    api: Api,
    scan: int,
    flags: int,
    *,
    hold: float = KEY_HOLD,
    sleep: Callable[[float], object] = time.sleep,
) -> None:
    """Key-down, ``hold`` seconds, key-up. Once the key-down has been attempted the key-up is
    sent whatever happens (a failed send, Ctrl+C during the hold); a send that SendInput did not
    insert raises OSError."""
    try:
        _send(api, scan, flags, "down")
        sleep(hold)
    except BaseException as e:
        try:
            _send(api, scan, flags | KEYEVENTF_KEYUP, "up")
        except Exception as up:
            e.add_note(f"the key-up failed too: {up}")
        raise
    _send(api, scan, flags | KEYEVENTF_KEYUP, "up")


# -- elevation (UIPI) -------------------------------------------------------------------------


@dataclass(frozen=True)
class Elevation:
    reactor: bool | None  # sound-reactor runs elevated (as administrator)
    target: bool | None  # the target window's process does (None: could not be read)
    error: str = ""  # why the target's could not be read


def _elevated(api: Api, pid: int | None) -> tuple[bool | None, str]:
    try:
        return api.process_elevated(pid), ""
    except OSError as e:
        return None, str(e) or type(e).__name__


def elevation(api: Api, pid: int) -> Elevation:
    reactor, _ = _elevated(api, None)
    target, error = _elevated(api, pid)
    return Elevation(reactor, target, error)


SAME_LEVEL = (
    "start the game without administrator rights, or sound-reactor from a terminal opened with "
    '"Run as administrator"'
)


def uipi_problem(e: Elevation, title: str) -> str | None:
    """Why Windows would drop the keys sent to ``title`` (UIPI), or None."""
    if e.reactor:  # an elevated sender reaches normal and elevated windows alike
        return None
    if e.target:
        return (
            f"{title!r} runs as administrator and sound-reactor does not: Windows would drop "
            f"every key (UIPI); {SAME_LEVEL}"
        )
    if e.target is None:
        return (
            f"cannot check whether {title!r} runs as administrator ({e.error}). That happens "
            "when it does, but also when it runs under another user account, when anti-cheat "
            "software protects its process, or when the process has just ended; if it runs as "
            "administrator, Windows would drop every key (UIPI). Run sound-reactor from a "
            'terminal opened with "Run as administrator": then keys reach it in every case'
        )
    return None


def window_problems(title: str, api: Api | None = None) -> list[str]:
    """What would keep keys from reaching the window ``title`` (if it is open)."""
    api = api or default_api()
    hwnd = find_window(api, title)
    if hwnd is None:
        return []
    problem = uipi_problem(elevation(api, api.window_thread_process(hwnd)[1]), title)
    return [problem] if problem else []


# -- the target window ------------------------------------------------------------------------


class Target:
    """The window that receives keys: focus check, held-input check and key presses (an engine
    ``Keyboard``)."""

    def __init__(
        self,
        api: Api,
        hwnd: int,
        title: str,
        *,
        hold: float = KEY_HOLD,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self.api = api
        self.hwnd = hwnd
        self.title = title
        self.thread_id, self.pid = api.window_thread_process(hwnd)
        self.hold = hold
        self.sleep = sleep

    @classmethod
    def find(cls, title: str, api: Api | None = None) -> Target:
        api = api or default_api()
        hwnd = find_window(api, title)
        if hwnd is None:
            raise LookupError(f"no window titled {title!r} is open")
        return cls(api, hwnd, title)

    def focused(self) -> bool:
        """The foreground window is this one: the same handle and the same process (a handle
        can be reused by another program's window after the game's has closed)."""
        hwnd = self.api.foreground_window()
        return hwnd == self.hwnd and self.api.window_thread_process(hwnd)[1] == self.pid

    def held(self) -> str | None:
        return held_input(self.api)

    def layout(self) -> int:
        """The keyboard layout of the window's thread (that of this thread if unknown)."""
        return self.api.keyboard_layout(self.thread_id) or self.api.keyboard_layout(0)

    def scan_code(self, key: str) -> tuple[int, bool]:
        return scan_code(self.api, key, self.layout())

    def keycode(self, key: str) -> int:
        """The scan code of ``key``, with 0xE000 added for an extended key."""
        code, extended = self.scan_code(key)
        return code | (0xE000 if extended else 0)

    def describe_key(self, key: str) -> str:
        code, extended = self.scan_code(key)
        return f"scan code 0x{code:02X}{' (extended)' if extended else ''}"

    def describe_layout(self) -> str:
        """``0x04070407 (de-DE)``: the keyboard layout keys are looked up in."""
        layout = self.layout()
        return f"0x{layout & 0xFFFFFFFF:08X} ({self.api.layout_name(layout) or 'unknown'})"

    def problems(self) -> list[str]:
        problem = uipi_problem(elevation(self.api, self.pid), self.title)
        return [problem] if problem else []

    def press(self, key: str) -> None:
        """Send ``key`` if this window is the foreground window right now (SendInput reaches
        the foreground window, whichever it is); :class:`KeyNotSent` otherwise."""
        scan, extended = self.scan_code(key)
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0)
        if not self.focused():
            raise KeyNotSent("the target window lost focus right before the key")
        send_key(self.api, scan, flags, hold=self.hold, sleep=self.sleep)
        if not self.focused():
            raise FocusLostDuringKey(
                "the target window lost focus while the key was held: its key-up went to the "
                "window in front now"
            )

    def close(self) -> None:
        pass


# -- keeping the screen on --------------------------------------------------------------------


class ExecutionStateInhibitor:
    """Keeps the display and the system on (``SetThreadExecutionState``) between
    :meth:`inhibit` and :meth:`release`: an engine ``Inhibitor``.

    The state belongs to the thread that sets it, so a thread of its own sets it, waits for
    :meth:`release` and clears it again; :meth:`inhibit` and :meth:`release` may be called from
    any thread. If the process ends, the state ends with it.
    """

    TIMEOUT = 5.0  # s
    # what the run log says (keys sent with SendInput count as user activity, so the screen
    # does not lock by itself during a run; this keeps the display from turning off)
    texts: ClassVar[dict[str, str]] = {
        "held": "display kept on while running",
        "released": "display no longer kept on",
        "failed": "could not keep the display on ({error}); it may turn off during the run",
        "release_failed": "could not stop keeping the display on: {error}",
    }

    def __init__(self, api: Api | None = None) -> None:
        self._api = api
        self._thread: threading.Thread | None = None
        self._release = threading.Event()
        self._cleared: list[int] = []

    def inhibit(self, reason: str) -> str:
        if self._thread is not None:
            return "already held"
        api = self._api or default_api()
        taken: queue.Queue[int] = queue.Queue()
        release = threading.Event()
        cleared: list[int] = []

        def hold() -> None:
            previous = 0
            try:
                previous = api.set_execution_state(AWAKE)
            finally:
                taken.put(previous)
            if previous:
                release.wait()
            cleared.append(api.set_execution_state(ES_CONTINUOUS))

        thread = threading.Thread(target=hold, name="sound-reactor keep awake", daemon=True)
        thread.start()
        try:
            previous = taken.get(timeout=self.TIMEOUT)
        except queue.Empty:
            release.set()
            raise TimeoutError("SetThreadExecutionState did not return") from None
        if not previous:
            thread.join(self.TIMEOUT)
            raise OSError("SetThreadExecutionState failed")
        self._thread, self._release, self._cleared = thread, release, cleared
        return "SetThreadExecutionState, display and system kept on"

    def release(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._release.set()
        thread.join(self.TIMEOUT)
        if thread.is_alive():
            raise TimeoutError("the thread keeping the screen on did not end")
        if not (self._cleared and self._cleared[0]):
            raise OSError("SetThreadExecutionState could not clear the state")


# -- closing the console ----------------------------------------------------------------------


def console_close_handler(
    stop: Callable[[str], object], wait: Callable[[float], object], timeout: float = CLOSE_WAIT
) -> Callable[[int], bool]:
    """A console control handler: on closing the console, logging off or shutting down, stop
    the run and wait up to ``timeout`` s for it to finish (the key-up, the log and the
    counters); Windows ends the process once the handler returns. Ctrl+C and Ctrl+Break are left
    to Python (False: not handled here)."""

    def handle(event: int) -> bool:
        reason = CLOSE_REASONS.get(event)
        if reason is None:
            return False
        try:
            stop(reason)
            wait(timeout)
        except Exception:
            pass  # nothing can be reported any more: the console is going away
        return True

    return handle


def install_console_handler(handler: Callable[[int], bool]) -> Callable[[], object]:
    """SetConsoleCtrlHandler(handler); returns the function that removes it again."""
    from ctypes import wintypes as w

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    routine = ctypes.WINFUNCTYPE(w.BOOL, w.DWORD)
    set_handler = kernel32.SetConsoleCtrlHandler
    set_handler.restype, set_handler.argtypes = w.BOOL, [routine, w.BOOL]
    callback = routine(lambda event: bool(handler(event)))  # referenced until removed
    if not set_handler(callback, True):
        raise ctypes.WinError(ctypes.get_last_error())  # pyright: ignore
    return lambda: set_handler(callback, False)


@contextlib.contextmanager
def on_console_close(
    stop: Callable[[str], object],
    wait: Callable[[float], object],
    *,
    install: Callable[[Callable[[int], bool]], Callable[[], object]] | None = None,
):
    """While inside: closing the console window (or logging off, shutting down) stops the run
    and gives it :data:`CLOSE_WAIT` s to finish. Does nothing outside Windows."""
    if install is None and sys.platform != "win32":
        yield
        return
    try:
        remove = (install or install_console_handler)(console_close_handler(stop, wait))
    except OSError:
        remove = None  # no console (e.g. started without one): nothing to guard
    try:
        yield
    finally:
        if remove is not None:
            remove()


# -- the backend ------------------------------------------------------------------------------


def backend(api: Api | None = None) -> Backend:
    """The Windows desktop (``api``: the Windows calls; the real ones by default)."""

    def windows() -> Api:
        return api or default_api()

    def target_problems(target: object) -> list[str]:
        return target.problems() if isinstance(target, Target) else []

    def target_about(target: object) -> list[str]:
        if not isinstance(target, Target):
            return []
        return [f"keyboard layout of the window: {target.describe_layout()}"]

    def about() -> list[str]:
        try:
            elevated, error = _elevated(windows(), None)
        except Exception as e:  # not on Windows
            elevated, error = None, str(e)
        answer = {True: "yes", False: "no"}.get(elevated, f"unknown ({error})")  # pyright: ignore
        return [f"sound-reactor runs as administrator: {answer}"]

    return Backend(
        name="Windows",
        sends_with="SendInput (scan codes) to the foreground window",
        reach="the Windows desktop",
        window_titles=lambda: window_titles(api),
        find_target=lambda title: Target.find(title, windows()),
        key_error=key_error,
        target_problems=target_problems,
        target_about=target_about,
        window_problems=lambda title: window_problems(title, windows()),
        app_windows=lambda pid: app_windows(windows(), pid),
        inhibitor=lambda: ExecutionStateInhibitor(api),
        about=about,
    )
