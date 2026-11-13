"""``sound-reactor doctor`` with fake desktops, fake audio applications and a test library."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sound_event_detector import Library
from sound_event_detector.capture import AppStream

from fakes import RATE, FakeTarget, chirp
from sound_reactor import desktop, doctor, keys, win32
from sound_reactor import profile as profiles
from sound_reactor.cli import main
from sound_reactor.profile import Key, Profile, SoundRule, Wait
from winfakes import FakeWindows

EXAMPLES = Path(__file__).parent.parent / "examples"
X_ONLY = {"KP_Home"}  # X key names outside the portable set, which X11 knows
APPS = [
    AppStream(1, "World of Warcraft", 100, "Wow.exe", "World of Warcraft", "active"),
    AppStream(2, "Firefox", 200, "firefox", "A private tab title"),
]


def fake_backend(target: FakeTarget | None = None, windows: tuple[str, ...] = ()):
    """An X11-like desktop with one window (``target``) and some other titles."""

    def find(title: str) -> FakeTarget:
        if target is None or title != target.title:
            raise LookupError(title)
        return target

    return desktop.Backend(
        name="X11",
        sends_with="XSendEvent to the window",
        reach="the X server",
        window_titles=lambda: list(windows),
        find_target=find,
        key_error=lambda key: (
            None if keys.is_portable(key) or key in X_ONLY else f"unknown key {key!r}"
        ),
        target_problems=lambda t: [],
        target_about=lambda t: [],
        window_problems=lambda title: [],
        app_windows=lambda pid: [],
        inhibitor=lambda: pytest.fail("the doctor must not inhibit anything"),
        about=lambda: ["DISPLAY :1, session wayland"],
    )


def fishing(source: str = "app:World of Warcraft", target: str = "Game") -> Profile:
    return Profile(
        source=source,
        target=target,
        sounds={"bite": {"min_level_db": -40.0}},
        on_start=[Key("9")],
        on_sound=[SoundRule("bite", [Wait(0.25), Key("f"), Wait(2.5), Key("9")])],
    )


@pytest.fixture
def library(tmp_path) -> Library:
    lib = Library(tmp_path / "library")
    lib.add("bite", chirp(), RATE)
    lib.add("bite", chirp(), RATE)
    lib.update_settings("bite", {"pitch_tolerance": 0.06, "template_length": 0.3})
    return lib


def report(name: str | None = "fishing", **kwargs) -> str:
    kwargs.setdefault("list_apps", lambda: list(APPS))
    return doctor.report(name, now=lambda: datetime(2026, 11, 13, 15, 0), **kwargs)


def test_a_profile_that_is_ready(library):
    profiles.save(fishing(), profiles.resolve("fishing"))
    text = report(backend=fake_backend(FakeTarget()), library=library)
    assert text.isascii()
    lines = text.splitlines()
    assert lines[0] == "sound-reactor doctor, 2026-11-13 15:00"
    assert lines[2].startswith("sound-reactor 0.1.0")
    assert lines[3].startswith("sound-event-detector ")
    assert lines[4].startswith("Python 3.") and ", Qt 6." in lines[4]
    for expected in (
        "desktop: X11, keys sent with XSendEvent to the window",
        "  DISPLAY :1, session wayland",
        "  app:World of Warcraft  (pid 100, Wow.exe, active)",
        "  app:Firefox  (pid 200, firefox)",
        "profiles: fishing",
        "  source app:World of Warcraft: playing audio (pid 100, Wow.exe, active)",
        "  target 'Game': window found, focused: yes",
        "    9: keycode 10",
        "    f: keycode 10",
        "    bite: 2 clips; threshold 0.75, min level -40 dBFS*, pitch +/-6 %, length 0.3 s, "
        "cooldown 3 s",
        "  start: ready",
        "warnings: none",
    ):
        assert expected in lines, expected
    assert "private tab" not in text  # what an application plays stays out of the report


def test_what_blocks_a_start_is_listed(library):
    p = fishing(source="app:Nothing", target="World of Warcraft")
    p.sounds["door"] = {}
    p.on_start = [Key("exclam"), Key("9")]
    p.start_delay = 1.0
    profiles.save(p, profiles.resolve("fishing"))
    windows = ("World of Warcraft - loading", "Konsole")
    text = report(backend=fake_backend(None, windows), library=library)
    assert "  source app:Nothing: NOT playing audio now" in text
    assert "  target 'World of Warcraft': NO such window is open" in text
    assert "    similar titles: 'World of Warcraft - loading'" in text
    assert "  keys: exclam, 9, f (checked against the keyboard layout once" in text
    assert "    door: NO clips in the detector library" in text
    assert "  start: blocked by 5 problem(s), see the warnings" in text
    warnings = text.split("warnings:\n")[1]
    for expected in (
        "start_delay must be at least 5 s",
        "unknown key 'exclam'",
        "nothing called 'Nothing' is playing audio now",
        "no window titled 'World of Warcraft' is open",
        "the sound 'door' has no clips",
    ):
        assert expected in warnings, expected


def test_keys_that_need_a_modifier_and_x_only_names(library):
    p = fishing()
    p.on_start = [Key("KP_Home"), Key("9")]
    profiles.save(p, profiles.resolve("fishing"))
    target = FakeTarget(bad_keys={"9"})
    text = report(backend=fake_backend(target), library=library)
    assert "    9: REFUSED" in text and "    KP_Home: keycode 10" in text
    assert "key '9' needs a modifier on this layout" in text
    assert "key 'KP_Home' is not one of the portable key names: it works on Linux only" in text
    assert "blocked by 1 problem(s)" in text  # the Linux-only name is a note, not a problem


def test_windows_shows_scan_codes_the_game_window_and_uipi(library):
    profiles.save(fishing(source="app:Wow.exe", target="World of Warcraft"),
                  profiles.resolve("fishing"))  # fmt: skip
    fake = FakeWindows(elevated={None: False, 1234: True})
    apps = [AppStream(0, "Wow.exe", 1234, "Wow.exe", "World of Warcraft", "active")]
    text = report(backend=win32.backend(fake), list_apps=lambda: apps, library=library)
    for expected in (
        "desktop: Windows, keys sent with SendInput (scan codes) to the foreground window",
        "  sound-reactor runs as administrator: no",
        "  app:Wow.exe  (pid 1234, active)",
        "  source app:Wow.exe: playing audio (pid 1234, active)",
        "    its windows: 'World of Warcraft'",
        "  target 'World of Warcraft': window found, focused: yes",
        "    keyboard layout of the window: 0x04090409 (en-US)",
        "    9: scan code 0x0A",
        "    f: scan code 0x21",
        "  administrator rights: MISMATCH",
    ):
        assert expected in text, expected
    assert "runs as administrator and sound-reactor does not" in text.split("warnings:")[1]
    fake.elevated[1234] = False
    text = report(backend=win32.backend(fake), list_apps=lambda: apps, library=library)
    assert "  administrator rights: OK (same level)" in text and "start: ready" in text


def test_the_windows_example_asks_for_the_game_executable(library):
    profiles.save(profiles.load(EXAMPLES / "wow-fishing-windows.toml"), profiles.resolve("w"))
    text = report("w", backend=win32.backend(FakeWindows()), library=library)
    assert "  source app:REPLACE-ME.exe: a placeholder" in text
    assert "replace 'app:REPLACE-ME.exe' in the profile with the game's entry" in text
    assert "length 0.3 s*" in text and "pitch +/-6 %*" in text


def test_without_a_profile_or_audio_applications(library):
    def broken() -> list[AppStream]:
        raise RuntimeError("pw-dump not found")

    text = report(None, backend=fake_backend(), list_apps=broken, library=library)
    assert "applications playing audio: cannot list them (RuntimeError: pw-dump" in text
    assert "profiles: none" in text and "profile " not in text.split("profiles: none")[1]
    assert "cannot list the applications playing audio" in text


def test_an_unreadable_profile(library):
    profiles.resolve("broken").parent.mkdir(parents=True, exist_ok=True)
    profiles.resolve("broken").write_text("[stop]\nmax_cycles = 0\n")
    text = report("broken", backend=fake_backend(), library=library)
    assert "profile broken: cannot be read" in text and "max_cycles must be" in text


def test_the_command(monkeypatch, capsys, library):
    monkeypatch.setattr(desktop, "current", lambda: fake_backend(FakeTarget()))
    monkeypatch.setattr(doctor, "playing_apps", lambda: list(APPS))
    monkeypatch.setenv("SOUND_EVENT_DETECTOR_HOME", str(library.root))
    profiles.save(fishing(), profiles.resolve("fishing"))
    assert main(["doctor", "fishing"]) == 0
    out = capsys.readouterr().out
    assert "  start: ready" in out and out.endswith("warnings: none\n")
    profiles.resolve("broken").write_text("not toml [")
    assert main(["doctor", "broken"]) == 2
    assert main(["doctor"]) == 0


def test_version(capsys):
    with pytest.raises(SystemExit) as done:
        main(["--version"])
    assert done.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("sound-reactor 0.1.0") and out.count("\n") == 1  # one line


def test_where_the_packages_came_from(monkeypatch):
    assert doctor.distribution("no-such-package-here") == "not installed"
    assert doctor.distribution("sound-reactor").startswith("0.1.0")

    class Dist:
        version = "0.1.0"

        def __init__(self, origin: dict) -> None:
            self.origin = origin

        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps(self.origin)

    origins = {
        "pinned": {"url": "https://github.com/zDju/sound-event-detector/archive/"
                   "24b02ced7c68a2bb004914c930df7b2182900bb7.zip", "archive_info": {}},
        "main": {"url": "https://github.com/zDju/sound-reactor/archive/refs/heads/main.zip",
                 "archive_info": {}},
        "git": {"url": "https://github.com/zDju/x", "vcs_info": {"commit_id": "abcdef0123"}},
        "editable": {"url": (Path.home() / "src" / "x").as_uri(), "dir_info": {"editable": True}},
    }  # fmt: skip
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: Dist(origins[name]))
    assert doctor.distribution("pinned") == "0.1.0 (github zDju/sound-event-detector @ 24b02ce)"
    assert doctor.distribution("main") == "0.1.0 (github zDju/sound-reactor @ refs/heads/main)"
    assert doctor.distribution("git") == "0.1.0 (git https://github.com/zDju/x @ abcdef0)"
    assert doctor.distribution("editable") == f"0.1.0 (editable, {Path('~/src/x')})"


def test_the_home_folder_is_never_printed(library):
    home = Path.home()
    p = fishing(source=f"file:{home / 'sr-doctor-test-missing' / 'rec.wav'}")
    profiles.save(p, profiles.resolve("fishing"))
    text = report(backend=fake_backend(FakeTarget()), library=library)
    shown = str(Path("~") / "sr-doctor-test-missing" / "rec.wav")
    assert f"  source file:{shown}: a recording" in text
    assert f"recording not found: {shown}" in text
    assert str(home) not in text
