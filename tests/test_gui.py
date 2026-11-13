"""The window, offscreen (conftest sets QT_QPA_PLATFORM=offscreen and removes DISPLAY).

Settings, the unsaved-changes question and the run-folder opener are injected: nothing here
writes to the user's configuration or opens anything.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from sound_event_detector import Library, save_wav
from sound_event_detector.capture import AppStream

from fakes import RATE, FakeClock, FakeKeyboard, FakeListener, chirp, make_replay
from sound_reactor import gui as gui_module
from sound_reactor import keys, paths
from sound_reactor import profile as profiles
from sound_reactor.engine import ARMED, REACTING, STARTING, STOPPED, WAITING, Engine, Status
from sound_reactor.profile import Key, Profile, SilenceRule, SoundRule, StopRules, Wait
from sound_reactor.runner import Run, RunError

pytest.importorskip("PySide6")
from PySide6.QtCore import QEventLoop, QSettings, QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QGroupBox,
    QPushButton,
    QWidget,
)

from sound_reactor.gui import (
    COL_LENGTH,
    COL_PITCH,
    COL_THRESHOLD,
    COLOURS,
    MainWindow,
    MeterBar,
    status_text,
)

EXAMPLE = Path(__file__).parent.parent / "examples" / "wow-fishing.toml"
APPS = [  # what PipeWire lists: application name and binary
    AppStream(1, "World of Warcraft", 100, "Wow.exe", "World of Warcraft"),
    AppStream(2, "Firefox", 200, "firefox", "YouTube"),
]
WINDOWS = ["World of Warcraft", "Konsole"]


@pytest.fixture(scope="module")
def app():
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    application = QApplication.instance() or QApplication([])
    assert QGuiApplication.platformName() == "offscreen"
    return application


@pytest.fixture
def make_window(app, tmp_path):
    made: list[MainWindow] = []
    opened: list[Path] = []

    def make(**kwargs) -> MainWindow:
        args = {
            "library": Library(tmp_path / "library"),
            "list_apps": lambda: list(APPS),
            "list_windows": lambda: list(WINDOWS),
            "window_problems": lambda title: [],
            "settings": QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat),
            "open_folder": opened.append,
            "ask_unsaved": lambda name: pytest.fail(f"unexpected question about {name}"),
        }
        args.update(kwargs)
        w = MainWindow(**args)
        w.opened = opened  # pyright: ignore[reportAttributeAccessIssue]
        w.show()  # offscreen: nothing appears on any display
        made.append(w)
        return w

    yield make
    for w in made:
        w.ask_unsaved = lambda name: "discard"
        w.close()


@pytest.fixture
def window(make_window):
    return make_window()


def wait_for(app, condition, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)


def in_profiles(name: str, p: Profile | None = None) -> Path:
    return profiles.save(p or profiles.load(EXAMPLE), profiles.resolve(name))


# -- the profile ---------------------------------------------------------------------------


def test_example_profile_round_trips_through_the_window(window):
    assert window.load_path(EXAMPLE)
    assert window.profile() == profiles.load(EXAMPLE)
    assert not window.dirty and window.windowTitle() == "wow-fishing — sound-reactor"
    assert window.sounds.item(0, 1).text() == "0"
    assert window.path_label.text() == str(EXAMPLE)


def test_values_survive_the_window_unchanged(window):
    p = Profile(
        source="app:World of Warcraft",
        target="World of Warcraft",
        sounds={
            "bite": {
                "threshold": 0.835,
                "min_level_db": -40.25,
                "pitch_tolerance": 0.065,
                "template_length": 0.333,
                "cooldown": 1.5,
            },
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
    assert window.profile() == p and not window.dirty


def test_threshold_edits_go_into_the_profile_not_the_library(window):
    window.library.add("chirp", chirp(), RATE)
    window.add_sound("chirp")
    spin = window.sounds.cellWidget(0, COL_THRESHOLD)
    assert isinstance(spin, QDoubleSpinBox)
    spin.setValue(0.9)
    assert window.profile().sounds == {"chirp": {"threshold": 0.9}}
    spin.setValue(0.75)  # the library's value again: no override
    assert window.profile().sounds == {"chirp": {}}
    assert window.library.settings("chirp")["threshold"] == 0.75
    assert not (window.library.path("chirp") / "event.toml").exists()
    texts = [b.text() for b in window.findChildren(QPushButton)]
    assert "Save to library" not in texts
    window.set_profile(Profile())  # drop the change


def test_pitch_and_length_are_edited_per_sound(window):
    window.library.add("chirp", chirp(), RATE)
    window.library.update_settings("chirp", {"pitch_tolerance": 0.06, "template_length": 0.3})
    window.add_sound("chirp")
    pitch = window.sounds.cellWidget(0, COL_PITCH)
    length = window.sounds.cellWidget(0, COL_LENGTH)
    assert isinstance(pitch, QDoubleSpinBox) and isinstance(length, QDoubleSpinBox)
    assert pitch.value() == 6.0 and pitch.suffix() == " %"  # shown in percent
    assert length.value() == 0.3
    assert window.profile().sounds == {"chirp": {}}  # the library's values: no overrides
    pitch.setValue(4.0)
    length.setValue(0.5)
    assert window.profile().sounds == {"chirp": {"pitch_tolerance": 0.04, "template_length": 0.5}}
    pitch.setValue(0.0)
    assert pitch.text() == "off"
    assert window.profile().sounds["chirp"]["pitch_tolerance"] == 0.0
    window.set_profile(Profile())


def test_use_offers_the_library_sounds(window):
    assert window.library_sounds.count() == 0
    window.library.add("chirp", chirp(), RATE)
    window.library.add("door", chirp(), RATE)
    window.refresh_sound_choices()
    choices = [window.library_sounds.itemText(i) for i in range(window.library_sounds.count())]
    assert choices == ["chirp", "door"]
    assert window.library_sounds.currentIndex() == -1  # nothing chosen for the user
    window._use_chosen_sound()  # nothing chosen: nothing added
    assert window.sounds.rowCount() == 0
    window.library_sounds.setCurrentIndex(0)
    window._use_chosen_sound()
    assert window._sound_name(0) == "chirp"
    choices = [window.library_sounds.itemText(i) for i in range(window.library_sounds.count())]
    assert choices == ["door"]  # already used sounds are not offered again
    window.set_profile(Profile())


def take(tmp_path: Path, name: str) -> Path:
    """A WAV recording with one chirp in silence."""
    wav = tmp_path / name
    padded = np.concatenate([np.zeros(RATE // 2), chirp(), np.zeros(RATE // 2)])
    save_wav(wav, padded.astype(np.float32), RATE)
    return wav


def test_new_sound_is_made_from_several_recordings(window, tmp_path):
    wavs = [take(tmp_path, "a.wav"), take(tmp_path, "b.wav")]
    asked: list[str] = []
    window.ask_sound_name = lambda: "splash"
    window.ask_wavs = lambda title: asked.append(title) or wavs
    assert window.new_sound_dialog()
    assert asked == ["Recordings of the new sound 'splash'"]
    assert window.library.names() == ["splash"]
    assert len(window.library.info("splash").clips) == 2
    assert window._sound_name(0) == "splash" and window.sounds.item(0, 1).text() == "2"
    window.set_profile(Profile())


def test_new_sound_refuses_bad_or_taken_names_and_cancel(window, tmp_path):
    window.library.add("chirp", chirp(), RATE)
    window.ask_wavs = lambda title: pytest.fail("no files should be asked for")
    window.ask_sound_name = lambda: None  # cancelled
    assert not window.new_sound_dialog()
    window.ask_sound_name = lambda: "two words"
    assert not window.new_sound_dialog()
    window.ask_sound_name = lambda: "chirp"
    assert not window.new_sound_dialog()
    log = window.log.toPlainText()
    assert "invalid sound name 'two words'" in log and "already has a sound 'chirp'" in log
    window.ask_sound_name = lambda: "fresh"
    window.ask_wavs = lambda title: []  # no files picked: no empty sound is made
    assert not window.new_sound_dialog()
    assert window.library.names() == ["chirp"] and window.sounds.rowCount() == 0


def test_add_clips_goes_to_the_selected_sound(window, tmp_path):
    window.library.add("chirp", chirp(), RATE)
    window.library.add("door", chirp(), RATE)
    window.add_sound("chirp")
    window.add_sound("door")
    window.ask_wavs = lambda title: pytest.fail("nothing is selected: no question")
    assert not window.add_clips_dialog()  # two sounds and none selected
    window.sounds.selectRow(1)
    window.ask_wavs = lambda title: [take(tmp_path, "x.wav"), take(tmp_path, "y.wav")]
    assert window.add_clips_dialog()
    assert len(window.library.info("door").clips) == 3
    assert window.sounds.item(1, 1).text() == "3" and window.sounds.item(0, 1).text() == "1"
    window.set_profile(Profile())


def test_a_new_rule_uses_the_sound_and_offers_the_others(window):
    window.library.add("chirp", chirp(), RATE)
    window.library.add("door", chirp(), RATE)
    window.add_sound("door")
    window._new_rule()
    assert window.rules.item(0, 0).text() == "door"  # the profile's sound, ready to use
    assert window.rules.currentColumn() == 1  # typing goes into the actions
    assert window.sound_choices() == ["door", "chirp"]
    delegate = window.rules.itemDelegateForColumn(0)
    index = window.rules.model().index(0, 0)
    editor = delegate.createEditor(window.rules.viewport(), None, index)
    delegate.setEditorData(editor, index)
    assert [editor.itemText(i) for i in range(editor.count())] == ["door", "chirp"]
    assert editor.currentText() == "door"
    editor.setCurrentText("chirp")
    delegate.setModelData(editor, window.rules.model(), index)
    assert window.rules.item(0, 0).text() == "chirp"
    window.rules.item(0, 1).setText("f")
    assert window.profile().on_sound == [SoundRule("chirp", [Key("f")])]
    window.set_profile(Profile())


def test_add_clip_from_a_wav(window, tmp_path):
    wav = tmp_path / "take.wav"
    padded = np.concatenate([np.zeros(RATE // 2), chirp(), np.zeros(RATE // 2)])
    save_wav(wav, padded.astype(np.float32), RATE)
    assert window.add_clip_from(wav, "chirp") is not None
    assert window.library.names() == ["chirp"]
    assert window.sounds.item(0, 1).text() == "1"
    window.set_profile(Profile())


def test_unsaved_changes_show_in_the_title_and_save_clears_them(window):
    path = in_profiles("fishing")
    window.load_path(path)
    window.on_start.setText("wait 1, 9")
    window.refresh_checks()
    assert window.dirty and window.windowTitle().startswith("fishing*")
    assert window.save()
    assert not window.dirty and window.windowTitle().startswith("fishing —")
    assert profiles.load(path).on_start == [Wait(1.0), Key("9")]


def test_unsaved_changes_are_asked_about(make_window):
    a, b = in_profiles("a"), in_profiles("b")
    answers: list[str] = []
    window = make_window(ask_unsaved=lambda name: answers.pop(0))
    window.refresh_profiles()
    window.load_path(a)
    index_b = window.profile_box.findText("b")

    window.on_start.setText("wait 1, 9")
    answers.append("cancel")
    window._profile_chosen(index_b)
    assert window.path == a and window.on_start.text() == "wait 1, 9"

    answers.append("discard")
    window._profile_chosen(index_b)
    assert window.path == b and profiles.load(a).on_start == [Key("9")]

    window.on_start.setText("wait 2, 9")
    answers.append("save")
    window._profile_chosen(window.profile_box.findText("a"))
    assert window.path == a and profiles.load(b).on_start == [Wait(2.0), Key("9")]

    window.on_start.setText("wait 3, 9")
    answers.append("cancel")
    assert not window.close() and window.isVisible()
    assert answers == []


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
    window.load_path(outside)
    window.on_start.setText("wait 5, 9")
    assert not window.save()  # a copy exists already: refused, nothing overwritten
    assert "Save as" in window.log.toPlainText()
    assert outside.read_bytes() == original
    window.set_profile(Profile())  # drop the change


def test_remembers_the_last_profile_and_the_window_size(make_window, tmp_path):
    path = in_profiles("fishing")
    first = make_window()
    first.load_path(path)
    first.resize(1000, 700)
    assert first.close()
    second = make_window()
    second.load_initial()
    assert second.path == path
    # The offscreen screen is only 800x600, so restoring clamps the width to fit it (down to
    # the window's minimum); the height shows that the saved geometry was applied.
    assert second.size().height() == 700 and second.size() != QSize(1150, 760)
    assert (tmp_path / "gui.ini").is_file()  # the injected settings, not the user's


def test_a_profile_that_is_not_text_is_refused(window, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"source = \xff\xfe\n")
    assert not window.load_path(bad)
    assert "not a UTF-8 text file" in window.log.toPlainText()


# -- source, target and problems -----------------------------------------------------------


def type_source(window, text: str) -> None:
    """Type ``text`` into the source box a character at a time, then finish editing."""
    line = window.source.lineEdit()
    for i in range(1, len(text) + 1):
        line.setText(text[:i])
        line.textEdited.emit(text[:i])
        assert window.target.currentText() == "", f"filled in while typing {text[:i]!r}"
    line.editingFinished.emit()


def pick_source(window, text: str) -> None:
    window.source.setCurrentText(text)
    window.source.activated.emit(window.source.findText(text))


def test_an_app_source_fills_in_its_window(window):
    type_source(window, "app:Firefox")  # "app:f" must not pick "World of Warcraft"
    assert window.target.currentText() == ""  # no window is called Firefox
    pick_source(window, "app:World of Warcraft")
    assert window.target.currentText() == "World of Warcraft"
    pick_source(window, "app:Firefox")  # filled in before, no match now: cleared
    assert window.target.currentText() == ""
    window.target.setCurrentText("")
    type_source(window, "app:wow.exe")  # the binary finds "World of Warcraft"
    assert window.target.currentText() == "World of Warcraft"

    line = window.target.lineEdit()
    line.setText("Konsole")
    line.textEdited.emit("Konsole")  # typed by the user: left alone from now on
    pick_source(window, "app:Firefox")
    assert window.target.currentText() == "Konsole"
    window.set_profile(Profile())


def test_an_app_is_found_by_its_binary_as_capture_does(window):
    window.library.add("bite", chirp(), RATE)
    for source, playing in (("app:Wow.exe", True), ("app:wow", True), ("app:100", True),
                            ("app:Nothing", False)):  # fmt: skip
        window.set_profile(Profile(source=source, target="World of Warcraft", sounds={"bite": {}}))
        assert ("nothing called" not in window.problems_label.text()) == playing, source


def test_problems_are_shown_under_the_buttons(window):
    window.set_profile(
        Profile(
            source="app:Nothing",
            target="Missing",
            sounds={"bite": {}},
            on_start=[Key("9")],
        )
    )
    text = window.problems_label.text()
    assert "the sound 'bite' has no clips" in text
    assert "nothing called 'Nothing' is playing" in text
    assert "no window titled 'Missing'" not in text  # blocks only Start: shown apart, in grey
    assert window.start_only_label.text().startswith("Start only: no window titled 'Missing'")
    assert not window.start_run(live=True) and not window.start_run(live=False)
    assert window.run is None

    window.library.add("bite", chirp(), RATE)
    window.source.setCurrentText("app:World of Warcraft")
    window.target.setCurrentText("World of Warcraft")
    window.refresh_checks()
    assert window.problems_label.text() == "Ready to start."
    assert not window.start_only_label.isVisible()

    window.on_start.setText("wait soon")
    window.refresh_checks()
    assert "'wait' needs a number" in window.problems_label.text()
    window.set_profile(Profile())


def test_start_refuses_a_replay(window, tmp_path):
    wav, _ = make_replay(tmp_path, [], 1.0)
    window.set_profile(Profile(source=f"file:{wav}", sounds={"chirp": {}}, on_start=[Key("9")]))
    assert window.problems_label.text() == "Ready to listen."
    assert "run it as a dry run" in window.start_only_label.text()
    assert window.start_only_label.isVisible()
    assert not window.start_run(live=True)
    assert window.run is None


def test_problems_while_starting_are_shown(window, tmp_path, monkeypatch):
    wav, _ = make_replay(tmp_path, [], 1.0)
    window.set_profile(Profile(source=f"file:{wav}", sounds={"chirp": {}}))
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))  # run folders cannot be created (Linux)
    monkeypatch.setenv("LOCALAPPDATA", str(blocker))  # (Windows)
    assert not window.start_run(live=False)
    text = window.problems_label.text()
    # Linux: NotADirectoryError; Windows: FileExistsError (the file is where the folder goes)
    assert "NotADirectoryError" in text or "FileExistsError" in text, text
    assert window.try_button.isEnabled()


# -- running -------------------------------------------------------------------------------


def test_try_without_keys_shows_status_counters_meter_and_summary(app, window, tmp_path):
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
    states: list[Status] = []
    texts: list[str] = []
    peaks: list[float] = []
    window.bridge.state.connect(states.append)
    window.bridge.state.connect(lambda s: texts.append(window.status_line.text()))
    window.bridge.meter.connect(lambda m: peaks.append(m.peaks["chirp"]))
    assert window.start_run(live=False)
    assert not window.start_button.isEnabled() and window.stop_button.isEnabled()
    wait_for(app, lambda: window.last_result is not None)

    phases = [s.phase for s in states]
    assert WAITING in phases and REACTING in phases and phases[-1] == STOPPED
    assert ARMED not in phases and STARTING not in phases  # no focus needed
    assert any(t.startswith("Waiting for a sound") and t.endswith("listening only") for t in texts)
    assert "Reacting: chirp · listening only" in texts
    assert window.status_line.text() == "Stopped: the audio source ended"

    v = window.counter_values
    assert v["cycles"].text() == "5" and v["triggers"].text() == "chirp 3"
    assert v["silent"].text() == "2 (0 in a row)" and v["hit_rate"].text() == "60 %"
    assert v["elapsed"].text() == "0:50"
    assert v["last"].text().startswith("chirp · ") and "similarity" in v["last"].text()

    bar = window.meter_bars["chirp"]
    assert bar.threshold == 0.75 and max(peaks) >= 0.75
    assert window.level_label.text().endswith("dBFS")

    assert window.summary_box.isVisible()
    summary = window.summary.text()
    assert "the audio source ended (listening only)" in summary
    assert "5 cycles, 3 triggers (chirp 3), 2 silent, hit rate 60 %; 0:50 long" in summary
    window.open_folder_button.click()
    assert window.opened == [window.run.folder]
    assert window.start_button.isEnabled() and not window.stop_button.isEnabled()


def test_status_line_texts_and_colours(window):
    assert status_text(None) == "Not running"
    assert status_text(Status(ARMED, "Game")) == "Armed: switch into Game (Alt+Tab)"
    assert status_text(Status(STARTING, remaining=4.2)) == "Starting in 5 s"
    assert status_text(Status(WAITING, elapsed=12.7)) == "Waiting for a sound (12 s)"
    assert status_text(Status(REACTING, "bite")) == "Reacting: bite"
    assert status_text(Status(REACTING, "bite", dry_run=True)) == "Reacting: bite · listening only"
    assert status_text(Status(STOPPED, "done", dry_run=True)) == "Stopped: done"
    window._show_status(Status(STOPPED, "focus lost", error=True))
    assert COLOURS["error"][0] in window.status_line.styleSheet()
    window._show_status(Status(REACTING, "bite"))
    assert COLOURS[REACTING][0] in window.status_line.styleSheet()


def test_meter_bar(app):
    bar = MeterBar("bite", 0.8)
    bar.set_score(0.85)
    assert bar.above and bar.text() == "bite  0.85 / 0.80"
    bar.set_score(0.5)
    assert not bar.above
    bar.set_score(-1.0)  # nothing scored (too quiet)
    assert bar.score is None and not bar.above
    bar.resize(300, 26)
    assert not bar.grab().isNull()  # paints


# -- layout --------------------------------------------------------------------------------


def test_menu_advanced_and_buttons(window):
    assert set(window.actions_by_name) == {"new", "open", "save", "save_as", "replay", "quit"}
    assert not window.advanced.expanded and not window.record.isVisible()
    window.advanced.set_expanded(True)
    assert window.record.isVisible()
    texts = {b.text() for b in window.findChildren(QPushButton)}
    assert {"Start", "Listen only", "Stop", "Save", "Open run folder"} <= texts


@pytest.mark.parametrize("size", [QSize(1150, 760), QSize(1000, 700)])
def test_no_horizontal_scrolling(app, window, size):
    window.load_path(EXAMPLE)
    window.advanced.set_expanded(True)
    window.resize(size)
    app.processEvents()
    assert window.size() == size, layout_report(app, window)  # the window can be this small
    assert window.config_scroll.horizontalScrollBar().maximum() == 0


def layout_report(app, window) -> str:
    """What makes the window wide: the font and the widest widgets (for a failure on CI)."""
    font = app.font()
    screen = app.primaryScreen()
    dpi = screen.logicalDotsPerInch() if screen is not None else 0.0
    wide = sorted(
        (
            w.minimumSizeHint().width(),
            type(w).__name__,
            (getattr(w, "text", lambda: "")() or "")[:40],
        )
        for w in window.findChildren(QWidget)
        if w.isVisible()
    )[-8:]
    lines = [
        f"font {font.family()!r} {font.pointSizeF()}pt px={font.pixelSize()} dpi={dpi}",
        f"window minimum {window.minimumSizeHint().width()}",
        *(f"  {width} {kind} {text!r}" for width, kind, text in reversed(wide)),
    ]
    return "\n".join(lines)


# -- runs with fake audio and a fake keyboard ----------------------------------------------


class SlowListener(FakeListener):
    """Fake audio whose clock runs about twenty times faster than real time, so that a run
    lasts long enough to click or close the window during it."""

    def next(self, until):
        time.sleep(0.005)
        return super().next(until)


def live_profile() -> Profile:
    return Profile(
        source="app:World of Warcraft",
        target="World of Warcraft",
        sounds={"chirp": {}},
        on_start=[Key("9")],
        on_sound=[SoundRule("chirp", [Wait(0.25), Key("f"), Wait(2.5), Key("9")])],
        on_silence=SilenceRule(30.0, [Key("9")]),
    )


@pytest.fixture
def fake_runs(monkeypatch, tmp_path):
    """``prepare`` builds an engine on fakes; the keyboard is ``run.engine.keyboard``."""
    calls: list[dict] = []

    def prepare(p, **kwargs):
        calls.append(kwargs)
        clock = FakeClock()
        sounds = [(8.0, "chirp", 0.9), (30.0, "chirp", 0.9), (60.0, "chirp", 0.9)]
        engine = Engine(
            p,
            SlowListener(clock, sounds),
            FakeKeyboard(clock),
            clock,
            focused=None if kwargs["dry_run"] else (lambda: clock.t >= 1.0),
            dry_run=kwargs["dry_run"],
            **{k: v for k, v in kwargs.items() if k.startswith("on_")},
        )
        return Run(engine, tmp_path / "run", None)

    monkeypatch.setattr(gui_module, "prepare", prepare)
    return calls


@pytest.fixture
def ready_window(window):
    window.library.add("chirp", chirp(), RATE)
    window.set_profile(live_profile())
    assert window.problems_label.text() == "Ready to start."
    return window


def test_the_buttons_start_live_and_dry_runs(ready_window, monkeypatch):
    seen: list[bool] = []

    def spy(p, **kwargs):
        seen.append(kwargs["dry_run"])
        raise RunError("spy: not started")

    monkeypatch.setattr(gui_module, "prepare", spy)
    ready_window.start_button.click()
    ready_window.try_button.click()
    assert seen == [False, True]  # Start sends keys; Listen only does not
    assert "spy: not started" in ready_window.problems_label.text()


def test_stop_during_a_run(app, ready_window, fake_runs):
    ready_window.start_button.click()
    keyboard = ready_window.run.engine.keyboard
    wait_for(app, lambda: keyboard.pressed)  # armed, switched in, first key
    ready_window.stop_button.click()
    wait_for(app, lambda: ready_window.last_result is not None)
    assert ready_window.last_result.reason == "stopped from the window"
    assert ready_window.status_line.text() == "Stopped: stopped from the window"
    assert ready_window.start_button.isEnabled() and not ready_window.stop_button.isEnabled()
    assert not fake_runs[0]["dry_run"]


def test_closing_stops_the_run_before_asking(app, ready_window, fake_runs):
    ready_window.start_button.click()
    engine = ready_window.run.engine
    wait_for(app, lambda: engine.keyboard.pressed)
    ready_window.on_start.setText("wait 1, 9")  # an unsaved change
    seen = {}

    def ask(name: str) -> str:
        seen["running"] = engine.running
        seen["keys"] = len(engine.keyboard.pressed)
        time.sleep(0.5)  # the question stays open: about 10 s of the run's time
        seen["keys later"] = len(engine.keyboard.pressed)
        return "cancel"

    ready_window.ask_unsaved = ask
    assert not ready_window.close()  # cancelled: the window stays
    assert seen["running"] is False and seen["keys later"] == seen["keys"]
    wait_for(app, lambda: ready_window.last_result is not None)
    assert ready_window.last_result.reason == "the window is closing"
    assert ready_window.isVisible() and not engine.running


def test_new_and_open_ask_about_unsaved_changes(make_window):
    a, b = in_profiles("a"), in_profiles("b")
    asked: list[str] = []
    window = make_window(ask_unsaved=lambda name: asked.append(name) or "cancel")
    window.load_path(a)
    window.on_start.setText("wait 1, 9")
    window.new_profile()
    assert not window.open_profile(b)
    assert asked == ["a", "a"]
    assert window.path == a and window.on_start.text() == "wait 1, 9"
    window.ask_unsaved = lambda name: "discard"
    assert window.open_profile(b) and window.path == b


def test_the_last_profile_falls_back_when_gone_or_broken(make_window, tmp_path):
    a = in_profiles("a")
    window = make_window()
    window.settings.setValue("last_profile", str(tmp_path / "deleted.toml"))
    window.load_initial()
    assert window.path == a and "error" not in window.log.toPlainText()
    assert window.settings.value("last_profile") == str(a.resolve())  # stored absolute

    broken = profiles.resolve("broken")
    broken.write_text("[stop]\nmax_cycles = 0\n")
    second = make_window()
    second.settings.setValue("last_profile", str(broken))
    second.load_initial()
    assert second.path == a  # the next one that can be read
    assert second.log.toPlainText().count("error:") == 1  # tried once, not twice

    third = make_window()
    third.load_initial("typo")
    assert third.path is None
    assert "cannot read" in third.log.toPlainText()


def test_edits_are_checked_by_themselves(app, window):
    path = in_profiles("fishing")
    window.load_path(path)
    window.on_start.setText("wait 1, 9")
    window.source.setCurrentText("app:Nothing")
    assert not window.windowTitle().startswith("fishing*")  # not yet: debounced
    wait_for(app, lambda: window.windowTitle().startswith("fishing*"), timeout=3.0)
    assert "nothing called 'Nothing'" in window.problems_label.text()
    window.set_profile(Profile())


def test_the_log_follows_new_lines_unless_scrolled_up(app, window):
    bar = window.log.verticalScrollBar()
    for i in range(300):
        window.append_log(f"line {i}")
    # laid out (lazily) and followed to the end
    wait_for(app, lambda: bar.maximum() > 200 and bar.value() == bar.maximum(), timeout=3.0)
    bar.setValue(10)  # the user scrolls up to read
    for i in range(100):
        window.append_log(f"more {i}")
    app.processEvents()
    assert bar.value() == 10
    bar.setValue(bar.maximum())  # back at the end: follows again
    for i in range(100):
        window.append_log(f"again {i}")
    wait_for(app, lambda: bar.value() == bar.maximum(), timeout=3.0)


def test_the_dropdown_shows_only_the_loaded_profile(make_window, tmp_path):
    a, _ = in_profiles("a"), in_profiles("b")
    broken = profiles.resolve("c")
    broken.write_text("not toml [")
    window = make_window(ask_unsaved=lambda name: "cancel")
    window.refresh_profiles()
    window.load_path(a)
    assert window.profile_box.currentText() == "a"

    outside = tmp_path / "elsewhere" / "b.toml"  # same name as one in the list
    outside.parent.mkdir()
    outside.write_text(EXAMPLE.read_text())
    window.load_path(outside)
    assert window.profile_box.currentIndex() == -1

    window.new_profile()
    window.on_start.setText("wait 1, 9")
    window._profile_chosen(window.profile_box.findText("b"))  # cancelled
    assert window.profile_box.currentIndex() == -1 and window.path is None

    window.load_path(a)
    window.on_start.setText("wait 1, 9")
    window.ask_unsaved = lambda name: "discard"
    window._profile_chosen(window.profile_box.findText("c"))  # cannot be read
    assert window.path == a and window.profile_box.currentText() == "a"


# -- key names, what would drop the keys, where settings go --------------------------------


def test_the_action_fields_explain_steps_and_keys(window):
    (rules_box,) = [b for b in window.findChildren(QGroupBox) if b.title().startswith("Rules")]
    window._add_rule("bite", "9")
    tips = {
        "rules box": rules_box.toolTip(),
        "on start": window.on_start.toolTip(),
        "on silence": window.on_silence.toolTip(),
        "rules table": window.rules.toolTip(),
        "actions header": window.rules.horizontalHeaderItem(1).toolTip(),
        "actions cell": window.rules.item(0, 1).toolTip(),
    }
    for where, tip in tips.items():
        for part in ("wait SECONDS", "key KEY", "a-z", "0-9", "F1-F12", "Return", "Escape",
                     "Left", "KP_Enter", "bracketleft", "need Shift", "Ctrl+F"):  # fmt: skip
            assert part in tip, (where, part)
    assert tips["on start"] == keys.help_text(linux_note=sys.platform != "win32")
    window.set_profile(Profile())


def test_what_would_drop_the_keys_blocks_only_start(make_window):
    asked: list[str] = []

    def uipi(title: str) -> list[str]:
        asked.append(title)
        return [f"{title!r} runs as administrator and sound-reactor does not"]

    window = make_window(window_problems=uipi)
    window.library.add("bite", chirp(), RATE)
    window.set_profile(
        Profile(
            source="app:World of Warcraft",
            target="World of Warcraft",
            sounds={"bite": {}},
            on_start=[Key("9")],
        )
    )
    assert asked and set(asked) == {"World of Warcraft"}
    assert window.problems_label.text() == "Ready to listen."
    assert "runs as administrator" in window.start_only_label.text()
    assert not window.start_run(live=True) and window.run is None
    window.target.setCurrentText("Missing")  # not open: that is reported instead
    window.refresh_checks()
    assert "no window titled 'Missing'" in window.start_only_label.text()
    assert "administrator" not in window.start_only_label.text()
    window.set_profile(Profile())


def test_settings_go_to_the_config_folder(make_window):
    window = make_window(settings=None)
    assert Path(window.settings.fileName()) == paths.gui_settings_file()
    assert window.settings.format() == QSettings.Format.IniFormat
    window.settings.setValue("probe", 1)
    window.settings.sync()
    assert paths.gui_settings_file().is_file()  # in the test's temporary config folder


def test_a_failing_window_check_is_a_problem_line_not_an_exception(make_window):
    calls: list[str] = []

    def broken(title: str) -> list[str]:
        calls.append(title)
        raise OSError("[WinError 5] Access is denied")

    window = make_window(window_problems=broken)
    window.library.add("bite", chirp(), RATE)
    window.set_profile(
        Profile(
            source="app:World of Warcraft",
            target="World of Warcraft",
            sounds={"bite": {}},
            on_start=[Key("9")],
        )
    )
    window.refresh_checks()  # again: every refresh asks, none raises
    assert len(calls) >= 2
    assert window.problems_label.text() == "Ready to listen."
    assert (
        "cannot check whether keys would reach 'World of Warcraft': OSError: [WinError 5] "
        "Access is denied" in window.start_only_label.text()
    )
    assert not window.start_run(live=True) and window.run is None
    window.set_profile(Profile())


def library_choices(window) -> list[str]:
    return [window.library_sounds.itemText(i) for i in range(window.library_sounds.count())]


def test_a_removed_sound_is_offered_again(window):
    window.library.add("chirp", chirp(), RATE)
    window.library.add("door", chirp(), RATE)
    window.add_sound("chirp")
    window.refresh_sound_choices()
    assert library_choices(window) == ["door"]
    window.sounds.selectRow(0)
    window.remove_sound()
    assert window.sounds.rowCount() == 0
    assert library_choices(window) == ["chirp", "door"]
    window.set_profile(Profile())


def test_one_bad_recording_among_several_in_new_sound(window, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not a WAV file")
    window.ask_sound_name = lambda: "splash"
    window.ask_wavs = lambda title: [take(tmp_path, "a.wav"), bad, take(tmp_path, "b.wav")]
    assert window.new_sound_dialog()
    assert len(window.library.info("splash").clips) == 2  # the good ones, both
    assert window.sounds.item(0, 1).text() == "2"
    log = window.log.toPlainText()
    assert "error: clip not added" in log and "bad.wav" in log
    window.set_profile(Profile())


def test_a_new_sound_name_can_be_typed_for_a_rule(window):
    window.library.add("chirp", chirp(), RATE)
    window.add_sound("chirp")
    window._add_rule("chirp", "f")
    delegate = window.rules.itemDelegateForColumn(0)
    index = window.rules.model().index(0, 0)
    editor = delegate.createEditor(window.rules.viewport(), None, index)
    delegate.setEditorData(editor, index)
    editor.lineEdit().setText(" door ")  # typed: not one of the offered sounds
    delegate.setModelData(editor, window.rules.model(), index)
    assert window.rules.item(0, 0).text() == "door"
    p = window.profile()
    assert p.on_sound == [SoundRule("door", [Key("f")])] and "door" in p.sound_names()
    window.refresh_checks()
    assert "the sound 'door' has no clips in the detector library" in window.problems_label.text()
    window.set_profile(Profile())
