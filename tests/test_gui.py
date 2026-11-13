"""Smoke tests of the window, offscreen (conftest sets QT_QPA_PLATFORM=offscreen, no DISPLAY)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest
from sound_event_detector import Library, save_wav

from fakes import RATE, chirp, make_replay
from sound_reactor import profile as profiles
from sound_reactor.profile import Key, Profile, SilenceRule, SoundRule, StopRules, Wait

pytest.importorskip("PySide6")
from PySide6.QtCore import QEventLoop
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QDoubleSpinBox

from sound_reactor.gui import COL_LIVE, COL_THRESHOLD, MainWindow

EXAMPLE = Path(__file__).parent.parent / "examples" / "wow-fishing.toml"


@pytest.fixture(scope="module")
def app():
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    application = QApplication.instance() or QApplication([])
    assert QGuiApplication.platformName() == "offscreen"
    return application


@pytest.fixture
def window(app, tmp_path):
    w = MainWindow(
        library=Library(tmp_path / "library"),
        list_sources=lambda: ["app:Test Game"],
        list_windows=lambda: ["Test Window"],
    )
    w.show()  # offscreen: nothing appears on any display
    yield w
    w.close()


def wait_for(app, condition, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)


def test_example_profile_round_trips_through_the_window(window):
    assert window.load_path(EXAMPLE)
    assert window.profile() == profiles.load(EXAMPLE)
    assert window.sounds.item(0, 1).text() == "0 (not in the library)"
    assert window.source.findText("app:Test Game") >= 0
    assert window.target.findText("Test Window") >= 0


def test_edit_and_save(window, tmp_path):
    window.load_path(EXAMPLE)
    window.on_start.setText("wait 1, 9")
    window.silence_on.setChecked(False)
    window.max_cycles.setValue(0)
    path = profiles.resolve("edited")
    assert window.save_to(path)
    saved = profiles.load(path)
    assert saved.on_start == [Wait(1.0), Key("9")]
    assert saved.on_silence is None and saved.stop.max_cycles is None
    assert window.profile_box.findText("edited") >= 0
    window.on_start.setText("wait soon")
    assert not window.save_to(path)
    assert "error: not saved" in window.log.toPlainText()


def test_sounds_clips_and_library_settings(window, tmp_path):
    wav = tmp_path / "take.wav"
    padded = np.concatenate([np.zeros(RATE // 2), chirp(), np.zeros(RATE // 2)])
    save_wav(wav, padded.astype(np.float32), RATE)
    assert window.add_clip_from(wav, "chirp") is not None
    assert window.library.names() == ["chirp"]
    assert window.sounds.item(0, 1).text() == "1"
    spin = window.sounds.cellWidget(0, COL_THRESHOLD)
    assert isinstance(spin, QDoubleSpinBox)
    spin.setValue(0.9)
    assert window.profile().sounds == {"chirp": {"threshold": 0.9}}
    window.save_sound_to_library()
    assert window.library.settings("chirp")["threshold"] == 0.9
    assert window.profile().sounds == {"chirp": {}}


def test_dry_run_replay_from_the_window(app, window, tmp_path):
    wav, _ = make_replay(tmp_path, [5.0, 12.0, 40.0], 50.0)
    window.set_profile(
        Profile(
            source=f"file:{wav}",
            sounds={"chirp": {}},
            on_start=[Key("9")],
            on_sound=[SoundRule("chirp", [Wait(0.25), Key("f"), Wait(2.5), Key("9")])],
            on_silence=SilenceRule(10.0, [Key("9")]),
        )
    )
    assert window.dry_run.isChecked()
    assert window.start_run()
    wait_for(app, lambda: window.last_result is not None)
    result = window.last_result
    assert result is not None and result.counters.triggers == {"chirp": 3}
    assert window.counters.text().startswith("Stopped: the audio source ended")
    assert window.log.toPlainText().count("CHIRP") == 3
    assert window.sounds.item(0, COL_LIVE).text().endswith("dBFS")
    assert window.start_button.isEnabled() and not window.stop_button.isEnabled()


def test_a_replay_cannot_send_keys(window, tmp_path):
    wav, _ = make_replay(tmp_path, [], 1.0)
    window.set_profile(Profile(source=f"file:{wav}", target="Test Window", sounds={"chirp": {}}))
    window.dry_run.setChecked(False)
    assert not window.start_run()
    assert "run it as a dry run" in window.log.toPlainText()
    assert window.run is None


def test_values_survive_the_window_unchanged(window):
    p = Profile(
        source="app:Test Game",
        target="Test Window",
        sounds={
            "bite": {"threshold": 0.835, "min_level_db": -40.25, "cooldown": 1.5},
            "door": {"threshold": 0.75},  # equal to the library's value, still kept
        },
        on_start=[Key("9")],
        on_sound=[SoundRule("bite", [Wait(0.123456789), Key("f"), Wait(2.5), Key("9")])],
        on_silence=SilenceRule(45.5, [Key("9")]),
        stop=StopRules(max_minutes=0.25, max_silent_in_row=20_000, max_cycles=5_000_000),
        ignore_after_action=1.234,
        start_delay=7.125,
    )
    window.set_profile(p)
    assert window.profile() == p
    spin = window.sounds.cellWidget(0, COL_THRESHOLD)
    assert isinstance(spin, QDoubleSpinBox)
    spin.setValue(0.9)  # a change by the user is taken as shown
    assert window.profile().sounds["bite"]["threshold"] == 0.9
    spin.setValue(0.75)  # back to the library's value: no override
    assert "threshold" not in window.profile().sounds["bite"]


def test_save_never_overwrites_a_profile_opened_elsewhere(window, tmp_path):
    outside = tmp_path / "elsewhere" / "fishing.toml"
    outside.parent.mkdir()
    outside.write_text(EXAMPLE.read_text())
    original = outside.read_bytes()
    assert window.load_path(outside)
    window.on_start.setText("wait 1, 9")
    assert window.save()
    copy = profiles.resolve("fishing")
    assert outside.read_bytes() == original
    assert profiles.load(copy).on_start == [Wait(1.0), Key("9")]
    assert window.path == copy
    assert window.save()  # now it is in the profiles folder: saved in place
    window.load_path(outside)
    assert not window.save()  # a copy exists already: refused, nothing overwritten
    assert "use Save as" in window.log.toPlainText()
    assert outside.read_bytes() == original
    assert profiles.load(copy).on_start == [Wait(1.0), Key("9")]


def test_problems_while_starting_are_logged(window, tmp_path, monkeypatch):
    wav, _ = make_replay(tmp_path, [], 1.0)
    window.set_profile(Profile(source=f"file:{wav}", sounds={"chirp": {}}))
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))  # run folders cannot be created
    assert not window.start_run()
    assert "error: not started: NotADirectoryError" in window.log.toPlainText()
    assert window.start_button.isEnabled()


def test_a_profile_that_is_not_text_is_refused(window, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"source = \xff\xfe\n")
    assert not window.load_path(bad)
    assert "not a UTF-8 text file" in window.log.toPlainText()
