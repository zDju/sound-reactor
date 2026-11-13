"""The Windows backend against a simulated Windows (``winfakes.FakeWindows``), on any platform;
the last tests use the real Windows calls (except SendInput, which conftest refuses) and run on
Windows only."""

from __future__ import annotations

import ctypes
import os
import sys
import threading

import pytest

from fakes import FakeClock, FakeListener, ForbiddenKeyboard, make_replay
from sound_reactor import desktop, keys, win32
from sound_reactor.engine import Engine
from sound_reactor.profile import Key, Profile, SoundRule, StopRules, Wait
from sound_reactor.runner import RunError, prepare
from winfakes import GERMAN, US, FakeWindows, Window, keys_sent

SCANCODE, KEYUP, EXTENDED = (
    win32.KEYEVENTF_SCANCODE,
    win32.KEYEVENTF_KEYUP,
    win32.KEYEVENTF_EXTENDEDKEY,
)


def target(fake: FakeWindows | None = None, title: str = "World of Warcraft") -> win32.Target:
    fake = fake or FakeWindows()
    t = win32.Target.find(title, fake)
    t.sleep = lambda seconds: None
    return t


# -- structures ----------------------------------------------------------------------------


def test_structures_have_the_windows_sizes():
    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(win32.INPUT) == (40 if wide else 28)
    assert ctypes.sizeof(win32.KEYBDINPUT) == (24 if wide else 16)
    assert ctypes.sizeof(win32.MOUSEINPUT) == (32 if wide else 24)
    assert ctypes.sizeof(win32.TOKEN_ELEVATION) == 4
    event = win32.key_input(0x21, SCANCODE | KEYUP)
    assert (event.type, event.ki.wVk, event.ki.wScan, event.ki.dwFlags) == (1, 0, 0x21, 0x0A)


# -- windows and focus ---------------------------------------------------------------------


def test_window_list_and_lookup():
    fake = FakeWindows()
    # visible, titled, once each, front to back
    assert win32.list_windows(fake) == ["World of Warcraft", "Konsole"]
    assert win32.window_titles(fake) == ["World of Warcraft", "Konsole"]
    assert win32.find_window(fake, "World of Warcraft") == 0x20  # the frontmost of two
    assert win32.find_window(fake, "Hidden") is None
    assert win32.find_window(fake, "world of warcraft") is None  # exact title
    assert win32.app_windows(fake, 1234) == ["World of Warcraft"]
    with pytest.raises(LookupError, match="no window titled 'Nothing'"):
        win32.Target.find("Nothing", fake)


def test_focus_is_the_foreground_window():
    fake = FakeWindows()
    t = target(fake)
    assert (t.hwnd, t.pid, t.thread_id) == (0x20, 1234, 77)
    assert t.focused()
    fake.foreground = 0x40
    assert not t.focused()
    fake.foreground = 0x50  # the other window with the same title is not the target
    assert not t.focused()
    fake.foreground = 0x20
    fake.windows[1].pid = 4321  # the game closed and another program got the same handle
    assert not t.focused()


def test_window_titles_are_empty_without_windows():
    def broken() -> list[int]:
        raise OSError("EnumWindows failed")

    fake = FakeWindows()
    fake.top_level_windows = broken
    assert win32.window_titles(fake) == []


# -- key names and scan codes --------------------------------------------------------------


def test_every_portable_key_has_a_scan_code_on_a_us_layout():
    t = target()
    for name in sorted(keys.PORTABLE):
        assert t.keycode(name) & 0xFF, name
    assert t.describe_key("f") == "scan code 0x21"
    assert t.keycode("F") == t.keycode("f") == 0x21  # the same key, as on Linux
    assert t.keycode("9") == 0x0A and t.keycode("0") == 0x0B
    assert t.describe_key("Up") == "scan code 0x48 (extended)"
    assert t.scan_code("Return") == (0x1C, False)
    assert t.scan_code("KP_Enter") == (0x1C, True)  # same virtual key, extended scan code
    assert t.scan_code("KP_Divide") == (0x35, True)
    assert t.scan_code("slash") == (0x35, False)
    assert t.scan_code("Prior") == t.scan_code("Page_Up") == (0x49, True)
    assert t.scan_code("F12") == (0x58, False) and t.scan_code("space") == (0x39, False)


def test_every_portable_name_without_a_character_has_a_virtual_key():
    """The table the window lists (keys.GROUPS) and the Windows mapping cannot drift apart."""
    for name in keys.PORTABLE:
        assert keys.character(name) is not None or name in win32.VK_OF, name
    assert set(win32.VK_OF) <= keys.PORTABLE


def test_names_outside_the_portable_set_are_unknown_on_windows():
    t = target()
    for name in ("exclam", "KP_0", "KP_Home", "XF86AudioPlay", "return", "f1", "Shift_L"):
        assert win32.key_error(name) is not None
        with pytest.raises(ValueError, match=r"unknown key .* on Windows use"):
            t.keycode(name)
    assert win32.key_error("F1") is None and win32.key_error("bracketleft") is None


def test_keys_that_need_a_modifier_are_refused():
    fake = FakeWindows(layouts={0: US, 77: GERMAN})  # the game's thread has a German layout
    t = target(fake)
    with pytest.raises(ValueError, match=r"'slash' needs a modifier on this layout \(Shift\)"):
        t.keycode("slash")
    with pytest.raises(ValueError, match=r"'backslash' needs a modifier .*\(AltGr\)"):
        t.keycode("backslash")
    with pytest.raises(ValueError, match="modifiers are not sent"):
        t.keycode("equal")
    # the layout is the target window's, not this thread's: Z is where Y is on US keyboards
    assert t.scan_code("z") == (0x15, False) and t.scan_code("y") == (0x2C, False)
    assert t.keycode("minus") == 0x0C and t.keycode("9") == 0x0A


def test_a_key_missing_from_the_layout_is_refused():
    fake = FakeWindows(layouts={0: 0x0BAD, 77: 0x0BAD})  # a layout the fake knows nothing of
    with pytest.raises(ValueError, match="not on the current keyboard layout"):
        target(fake).keycode("f")
    with pytest.raises(ValueError, match="not on the current keyboard layout"):
        target(fake).keycode("F1")


def test_the_layout_is_described():
    assert target().describe_layout() == "0x04090409 (en-US)"
    fake = FakeWindows(layouts={0: US, 77: GERMAN})
    assert target(fake).describe_layout() == "0x04070407 (de-DE)"
    fake = FakeWindows(layouts={0: 0x0BAD0BAD, 77: 0x0BAD0BAD})
    assert target(fake).describe_layout() == "0x0BAD0BAD (unknown)"


def test_an_unknown_target_layout_falls_back_to_this_thread():
    fake = FakeWindows(layouts={0: GERMAN})  # the target's thread has none
    assert target(fake).scan_code("z") == (0x15, False)


# -- sending keys --------------------------------------------------------------------------


def test_press_sends_the_scan_code_down_then_up():
    fake = FakeWindows()
    holds: list[float] = []
    t = target(fake)
    t.sleep = holds.append
    t.press("f")
    assert fake.sent == [(1, 0, 0x21, SCANCODE), (1, 0, 0x21, SCANCODE | KEYUP)]
    assert holds == [win32.KEY_HOLD]
    fake.sent.clear()
    t.press("Left")
    assert keys_sent(fake) == [("down", 0x4B, True), ("up", 0x4B, True)]
    assert fake.sent[0][3] == SCANCODE | EXTENDED


def test_focus_is_checked_right_before_each_key():
    fake = FakeWindows()
    t = target(fake)
    fake.foreground = 0x40
    with pytest.raises(win32.KeyNotSent, match="lost focus right before the key"):
        t.press("f")
    assert fake.sent == []


def test_focus_lost_between_the_engines_check_and_the_key():
    """The engine's own focus check passes; by the time of the backend's, a moment later,
    another window is in front."""
    fake = FakeWindows(foreground=0x40)
    t = target(fake)
    clock = FakeClock()
    p = Profile(source="app:x", target="World of Warcraft", on_start=[Key("9")])
    engine = Engine(p, FakeListener(clock), t, clock, focused=lambda: clock.t >= 2.0)
    result = engine.run()
    assert result.reason == ("the target window lost focus right before the key (key '9' not sent)")
    assert not result.error and fake.sent == [] and result.counters.keys == 0


def test_focus_lost_while_the_key_is_held():
    fake = FakeWindows()
    t = target(fake)
    t.sleep = lambda seconds: setattr(fake, "foreground", 0x40)  # Alt+Tab during the hold
    with pytest.raises(win32.FocusLostDuringKey, match="lost focus while the key was held"):
        t.press("f")
    assert keys_sent(fake) == [("down", 0x21, False), ("up", 0x21, False)]


def test_a_run_stops_with_a_warning_when_focus_goes_during_a_key():
    fake = FakeWindows()
    clock = FakeClock()
    fake.foreground = lambda: 0x20 if clock.t >= 2.0 else 0x40
    t = target(fake)
    t.sleep = lambda seconds: setattr(fake, "foreground", 0x40)
    p = Profile(source="app:x", target="World of Warcraft", on_start=[Key("9")])
    logs: list[str] = []
    result = Engine(p, FakeListener(clock), t, clock, focused=t.focused, on_log=logs.append).run()
    assert result.reason == "the target window lost focus while key '9' was held"
    assert not result.error and result.counters.keys == 1  # it was sent
    assert any(line.endswith("  key 9") for line in logs)
    assert any("WARNING: the target window lost focus while the key was held" in x for x in logs)
    assert len(fake.sent) == 2


def test_the_key_up_follows_the_key_down_on_errors():
    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    fake = FakeWindows()
    t = target(fake)
    t.sleep = interrupted
    with pytest.raises(KeyboardInterrupt):
        t.press("f")
    assert keys_sent(fake) == [("down", 0x21, False), ("up", 0x21, False)]

    fake = FakeWindows(send_results=[0])  # SendInput inserts no key-down
    with pytest.raises(OSError, match=r"did not insert the key-down \(Windows error 5\)"):
        target(fake).press("f")
    assert keys_sent(fake) == [("down", 0x21, False), ("up", 0x21, False)]

    fake = FakeWindows(send_results=[1, 0])  # the key-up is not inserted
    with pytest.raises(OSError, match="did not insert the key-up"):
        target(fake).press("f")
    assert len(fake.sent) == 2

    fake = FakeWindows(send_results=[0, 0])
    with pytest.raises(OSError, match="key-down") as failed:
        target(fake).press("f")
    assert any("key-up failed too" in note for note in failed.value.__notes__)


def test_a_failing_send_call_still_releases_the_key():
    fake = FakeWindows()

    def fail_on_down(event) -> None:
        if not event[3] & KEYUP:
            raise OSError("access violation")

    fake.on_send = fail_on_down
    with pytest.raises(OSError, match="access violation"):
        target(fake).press("9")
    assert keys_sent(fake) == [("down", 0x0A, False), ("up", 0x0A, False)]


def test_held_keys_and_mouse_buttons():
    fake = FakeWindows()
    t = target(fake)
    assert t.held() is None
    fake.held = {0x57, 0x10, 0xA0, 0x01, 0x02}  # W, Shift (generic and left), left and right
    assert t.held() == "key(s) w, Shift_L and mouse button(s) 1, 3"
    fake.held = {0x26, 0x05, 0xE9}
    assert t.held() == "key(s) Up, VK 0xE9 and mouse button(s) 8"


def test_a_held_key_stops_the_run_before_the_key_is_sent():
    fake = FakeWindows()
    clock = FakeClock()
    fake.foreground = lambda: 0x20 if clock.t >= 2.0 else 0x40
    fake.held = {0x57, 0x01}  # W and the left mouse button, e.g. walking with the mouse held
    t = target(fake)
    p = Profile(source="app:x", target="World of Warcraft", on_start=[Key("9")])
    result = Engine(p, FakeListener(clock), t, clock, focused=t.focused).run()
    assert result.error and fake.sent == []
    assert result.reason == "key(s) w and mouse button(s) 1 held before key '9'; not sent"


# -- elevation (UIPI) ----------------------------------------------------------------------


def test_an_elevated_game_blocks_a_normal_sound_reactor():
    fake = FakeWindows(elevated={None: False, 1234: True})
    (problem,) = target(fake).problems()
    assert "'World of Warcraft' runs as administrator and sound-reactor does not" in problem
    assert "UIPI" in problem and "Run as administrator" in problem
    assert win32.window_problems("World of Warcraft", fake) == [problem]
    assert win32.window_problems("Nothing", fake) == []


def test_elevation_that_cannot_be_read_blocks_too():
    denied = PermissionError(5, "Access is denied")
    fake = FakeWindows(elevated={None: False, 1234: denied})
    (problem,) = target(fake).problems()
    assert problem.startswith("cannot check whether 'World of Warcraft' runs as administrator")
    assert "Access is denied" in problem
    assert "another user account" in problem and "anti-cheat" in problem
    assert 'Run sound-reactor from a terminal opened with "Run as administrator"' in problem


@pytest.mark.parametrize(
    ("reactor", "game"),
    [(False, False), (True, True), (True, False), (True, PermissionError(5, "denied"))],
)
def test_no_problem_when_sound_reactor_is_at_least_as_elevated(reactor, game):
    fake = FakeWindows(elevated={None: reactor, 1234: game})
    assert target(fake).problems() == []


# -- keeping the screen on -----------------------------------------------------------------


def test_the_execution_state_is_taken_and_released_on_one_thread():
    fake = FakeWindows()
    inhibitor = win32.ExecutionStateInhibitor(fake)
    assert "SetThreadExecutionState" in inhibitor.inhibit("fishing")
    assert inhibitor.inhibit("again") == "already held"
    released = threading.Thread(target=inhibitor.release)  # from another thread: still fine
    released.start()
    released.join(5.0)
    (take_thread, take), (clear_thread, clear) = fake.execution
    assert take == win32.ES_CONTINUOUS | win32.ES_SYSTEM_REQUIRED | win32.ES_DISPLAY_REQUIRED
    assert clear == win32.ES_CONTINUOUS
    assert take_thread == clear_thread
    assert take_thread not in (threading.get_ident(), released.ident)
    inhibitor.release()  # nothing held any more
    assert len(fake.execution) == 2


def test_a_failed_take_raises_and_still_clears():
    fake = FakeWindows(execution_result=0)
    inhibitor = win32.ExecutionStateInhibitor(fake)
    with pytest.raises(OSError, match="SetThreadExecutionState failed"):
        inhibitor.inhibit("fishing")
    assert [flags for _, flags in fake.execution] == [win32.AWAKE, win32.ES_CONTINUOUS]
    assert len({thread for thread, _ in fake.execution}) == 1
    inhibitor.release()  # nothing held


def test_the_engine_takes_and_releases_it_around_a_live_run():
    fake = FakeWindows()
    clock = FakeClock()
    fake.foreground = lambda: 0x20 if 2.0 <= clock.t < 20.0 else 0x40
    t = target(fake)
    p = Profile(source="app:x", target="World of Warcraft", on_start=[Key("9")])
    inhibitor = win32.ExecutionStateInhibitor(fake)
    logs: list[str] = []
    engine = Engine(
        p, FakeListener(clock), t, clock, focused=t.focused, inhibitor=inhibitor,
        on_log=logs.append,
    )  # fmt: skip
    result = engine.run()
    assert result.reason == "the target window lost focus"
    assert [flags for _, flags in fake.execution] == [win32.AWAKE, win32.ES_CONTINUOUS]
    assert any("display kept on while running (SetThreadExecutionState" in line for line in logs)
    assert any("display no longer kept on" in line for line in logs)
    assert keys_sent(fake) == [("down", 0x0A, False), ("up", 0x0A, False)]


# -- the backend and a whole live run ------------------------------------------------------


def test_the_backend_is_chosen_by_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    backend = desktop.current()
    assert backend.name == "Windows" and backend.key_error("exclam") is not None
    assert backend.window_titles() == []  # conftest refuses the real desktop


def test_linux_uses_x11(monkeypatch):
    pytest.importorskip("Xlib")
    monkeypatch.setattr(sys, "platform", "linux")
    backend = desktop.current()
    assert backend.name == "X11" and backend.key_error("exclam") is None


def use_windows(monkeypatch, fake: FakeWindows) -> None:
    monkeypatch.setattr(desktop, "current", lambda: win32.backend(fake))


def live_profile() -> Profile:
    return Profile(
        source="app:Wow.exe",
        target="World of Warcraft",
        sounds={"chirp": {}},
        on_start=[Key("9")],
        on_sound=[SoundRule("chirp", [Wait(0.25), Key("f"), Wait(2.5), Key("9")])],
        stop=StopRules(max_cycles=1),
    )


def test_a_live_run_on_windows(tmp_path, monkeypatch):
    _, library = make_replay(tmp_path, [], 1.0)
    fake = FakeWindows()
    clock = FakeClock()
    fake.foreground = lambda: 0x20 if clock.t >= 2.0 else 0x40  # Alt+Tab into the game at 2 s
    use_windows(monkeypatch, fake)
    run = prepare(live_profile(), runs_root=tmp_path / "runs", library=library)
    engine = run.engine
    assert isinstance(engine.keyboard, win32.Target) and engine.focused == engine.keyboard.focused
    assert isinstance(engine.inhibitor, win32.ExecutionStateInhibitor)
    engine.keyboard.sleep = lambda seconds: None
    engine.listener = FakeListener(clock, [(15.0, "chirp", 0.9)])
    engine.clock = clock
    result = engine.run()
    assert "max_cycles" in result.reason
    assert [scan for kind, scan, _ in keys_sent(fake) if kind == "down"] == [0x0A, 0x21, 0x0A]
    assert [kind for kind, _, _ in keys_sent(fake)] == ["down", "up"] * 3
    assert [flags for _, flags in fake.execution] == [win32.AWAKE, win32.ES_CONTINUOUS]
    assert "SetThreadExecutionState" in (run.folder / "log.txt").read_text()


def test_live_prepare_on_windows_refuses_uipi_and_modifier_keys(tmp_path, monkeypatch):
    _, library = make_replay(tmp_path, [], 1.0)
    use_windows(monkeypatch, FakeWindows(elevated={None: False, 1234: True}))
    with pytest.raises(RunError, match="runs as administrator and sound-reactor does not"):
        prepare(live_profile(), runs_root=tmp_path / "runs", library=library)
    use_windows(monkeypatch, FakeWindows(layouts={0: US, 77: GERMAN}))
    p = live_profile()
    p.on_start = [Key("slash")]
    with pytest.raises(RunError, match="needs a modifier"):
        prepare(p, runs_root=tmp_path / "runs", library=library)
    p.on_start = [Key("exclam")]  # an X name: fine on Linux, unknown on Windows
    with pytest.raises(RunError, match="unknown key 'exclam': on Windows use"):
        prepare(p, dry_run=True, runs_root=tmp_path / "runs", library=library)
    use_windows(monkeypatch, FakeWindows(windows=[Window(0x99, "Konsole", 1, 1)]))
    with pytest.raises(RunError, match="no window titled 'World of Warcraft' is open"):
        prepare(live_profile(), runs_root=tmp_path / "runs", library=library)
    assert not (tmp_path / "runs").exists()


def test_a_failed_take_is_logged_and_the_run_goes_on():
    fake = FakeWindows(execution_result=0)
    clock = FakeClock()
    t = target(fake)
    p = Profile(source="app:x", target="World of Warcraft", on_start=[Key("9")])
    fake.foreground = lambda: 0x20 if 2.0 <= clock.t < 10.0 else 0x40
    logs: list[str] = []
    engine = Engine(
        p, FakeListener(clock), t, clock, focused=t.focused,
        inhibitor=win32.ExecutionStateInhibitor(fake), on_log=logs.append,
    )  # fmt: skip
    engine.run()
    assert any("WARNING: could not keep the display on (OSError" in line for line in logs)
    assert fake.sent  # the run went on


# -- closing the console -------------------------------------------------------------------


def test_closing_the_console_stops_the_run_and_waits_for_it():
    calls: list[tuple[str, object]] = []
    handler = win32.console_close_handler(
        lambda reason: calls.append(("stop", reason)), lambda s: calls.append(("wait", s))
    )
    assert handler(0) is False and handler(1) is False  # Ctrl+C, Ctrl+Break: Python's
    assert calls == []
    assert handler(win32.CTRL_CLOSE_EVENT) is True
    assert calls == [("stop", "the console window was closed"), ("wait", win32.CLOSE_WAIT)]
    assert handler(win32.CTRL_SHUTDOWN_EVENT) is True

    def broken(reason: str) -> None:
        raise RuntimeError("no run")

    assert win32.console_close_handler(broken, lambda s: None)(win32.CTRL_LOGOFF_EVENT) is True


def test_the_console_handler_is_installed_only_while_inside():
    events: list[str] = []
    handlers: list = []

    def install(handler):
        events.append("install")
        handlers.append(handler)
        return lambda: events.append("remove")

    with win32.on_console_close(lambda r: None, lambda s: None, install=install):
        assert events == ["install"] and handlers[0](win32.CTRL_CLOSE_EVENT) is True
    assert events == ["install", "remove"]

    def no_console(handler):
        raise OSError("no console")

    with win32.on_console_close(lambda r: None, lambda s: None, install=no_console):
        pass  # nothing to guard; no error


def test_a_closed_console_stops_a_real_run_quickly():
    """The handler stops the engine thread of a run in progress and waits for its end."""
    clock = FakeClock()
    p = Profile(source="app:x", target="World of Warcraft", on_start=[Key("9")])

    class Slow(FakeListener):
        def next(self, until):
            import time

            time.sleep(0.002)
            return super().next(until)

    engine = Engine(p, Slow(clock), ForbiddenKeyboard(), clock, dry_run=True)
    engine.start()
    handler = win32.console_close_handler(engine.stop, engine.join)
    assert handler(win32.CTRL_CLOSE_EVENT)
    assert not engine.running and engine.result is not None
    assert engine.result.reason == "the console window was closed"


# -- real Windows (never SendInput) --------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")


@windows_only
def test_real_windows_calls():
    api = win32.User32()
    windows = api.top_level_windows()
    assert all(isinstance(hwnd, int) for hwnd in windows)
    titles = win32.list_windows(api)
    assert all(isinstance(title, str) and title for title in titles)
    assert isinstance(api.foreground_window(), int)
    assert isinstance(api.process_elevated(None), bool)
    layout = api.keyboard_layout(0)
    assert layout != 0
    assert win32.scan_code(api, "F1", layout) == (0x3B, False)
    assert win32.scan_code(api, "Up", layout) == (0x48, True)
    for key, us in (("9", 0x0A), ("minus", 0x0C), ("f", 0x21)):
        try:
            code = win32.scan_code(api, key, layout)
        except ValueError as e:  # a layout where it needs a modifier (9 on AZERTY)
            assert "needs a modifier" in str(e) and layout & 0xFFFF != 0x0409
        else:
            assert code[0] and (layout & 0xFFFF != 0x0409 or code == (us, False)), key
    assert isinstance(api.process_elevated(os.getpid()), bool)
    assert api.layout_name(0x04090409) == "en-US"
    assert isinstance(api.key_down(0x41), bool)
    if windows:
        thread, pid = api.window_thread_process(windows[0])
        assert thread > 0 and pid > 0


@windows_only
def test_real_execution_state():
    inhibitor = win32.ExecutionStateInhibitor(win32.User32())
    assert "SetThreadExecutionState" in inhibitor.inhibit("test")
    inhibitor.release()


@windows_only
def test_real_console_handler_install_and_remove():
    remove = win32.install_console_handler(lambda event: False)
    remove()


@windows_only
def test_the_real_send_input_is_refused_in_tests():
    with pytest.raises(AssertionError, match="real key"):
        win32.User32().send_input([win32.key_input(0x21, SCANCODE)])
