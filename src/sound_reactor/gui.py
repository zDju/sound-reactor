"""The window: edit profiles, manage sounds, and start/stop runs of the same engine as the CLI.

Errors are shown in the log view, not in pop-ups; dialogs open only from the buttons that ask
for a file. The engine runs in its own thread and reaches the window through Qt signals.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sound_event_detector import Library, list_apps, load_wav
from sound_event_detector.audio import find_end, find_onset, to_mono, trim

from . import profile as profiles
from . import x11
from .engine import Counters, Meter, Result
from .profile import Profile, ProfileError, SilenceRule, SoundRule, StopRules
from .runner import Run, RunError, prepare

SHOWN_SETTINGS = ("threshold", "min_level_db")  # edited in the sounds table
COL_NAME, COL_CLIPS, COL_THRESHOLD, COL_MIN_LEVEL, COL_LIVE = range(5)


def app_sources() -> list[str]:
    """``app:NAME`` for each application playing audio now (PipeWire)."""
    try:
        return sorted({f"app:{s.name}" for s in list_apps()})
    except Exception:
        return []


class _Bridge(QObject):
    """Carries engine callbacks (engine thread) to the window (GUI thread)."""

    log = Signal(str)
    counters = Signal(object)
    meter = Signal(object)
    finished = Signal(object)


def _button(text: str, slot: Callable[[], object]) -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(slot)
    return b


def _row(*widgets: QWidget, stretch: bool = True) -> QHBoxLayout:
    layout = QHBoxLayout()
    for w in widgets:
        layout.addWidget(w)
    if stretch:
        layout.addStretch(1)
    return layout


def _put(spin: QSpinBox | QDoubleSpinBox, value: float | None) -> None:
    """Show ``value`` (None: 0, "off") and remember it exactly: spin boxes round to their
    decimals and clip to their range, and a profile must not change just by being saved."""
    spin.setValue(0 if value is None else value)  # pyright: ignore[reportArgumentType]
    spin.setProperty("exact", value)
    spin.setProperty("shown", spin.value())


def _get(spin: QSpinBox | QDoubleSpinBox) -> float | None:
    """The value given to :func:`_put` while the user has not changed the box, else the box's."""
    return spin.property("exact") if _unchanged(spin) else spin.value()


def _unchanged(spin: QSpinBox | QDoubleSpinBox) -> bool:
    return spin.value() == spin.property("shown")


def _spin(maximum: int, tip: str) -> QSpinBox:
    s = QSpinBox()
    s.setRange(0, maximum)
    s.setSpecialValueText("off")
    s.setToolTip(tip)
    return s


def _seconds(maximum: float, step: float = 0.5) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(0.0, maximum)
    s.setDecimals(2)
    s.setSingleStep(step)
    s.setSuffix(" s")
    return s


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        library: Library | None = None,
        list_sources: Callable[[], list[str]] = app_sources,
        list_windows: Callable[[], list[str]] = x11.window_titles,
    ) -> None:
        super().__init__()
        self.setWindowTitle("sound-reactor")
        self.resize(1150, 760)
        self.library = library or Library()
        self.list_sources = list_sources
        self.list_windows = list_windows
        self.path: Path | None = None
        self.run: Run | None = None
        self.last_result: Result | None = None
        self._hidden_overrides: dict[str, dict[str, float]] = {}  # not shown in the table
        self.bridge = _Bridge()
        self.bridge.log.connect(self.append_log)
        self.bridge.counters.connect(self._show_counters)
        self.bridge.meter.connect(self._show_meter)
        self.bridge.finished.connect(self._finished)

        config = QWidget()
        left = QVBoxLayout(config)
        left.addLayout(self._profile_bar())
        left.addWidget(self._source_box())
        left.addWidget(self._sounds_box())
        left.addWidget(self._rules_box())
        left.addWidget(self._stop_box())
        left.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(config)

        central = QWidget()
        main = QHBoxLayout(central)
        main.addWidget(scroll, 3)
        main.addLayout(self._run_panel(), 2)
        self.setCentralWidget(central)

        self.refresh_sources()
        self.refresh_windows()
        self.refresh_profiles()
        self.set_profile(Profile())

    # -- layout --------------------------------------------------------------------------------

    def _profile_bar(self) -> QHBoxLayout:
        self.profile_box = QComboBox()
        self.profile_box.setMinimumWidth(180)
        self.profile_box.activated.connect(self._profile_chosen)
        self.path_label = QLabel()
        bar = _row(
            QLabel("Profile"),
            self.profile_box,
            _button("New", self.new_profile),
            _button("Open…", self._open_dialog),
            _button("Save", self.save),
            _button("Save as…", self._save_as_dialog),
        )
        bar.addWidget(self.path_label)
        return bar

    def _source_box(self) -> QGroupBox:
        box = QGroupBox("Source and target")
        form = QFormLayout(box)
        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.setToolTip("app:NAME (one application), device:TEXT, default, or file:PATH.wav")
        form.addRow(
            "Listen to",
            _row(
                self.source,
                _button("Refresh", self.refresh_sources),
                _button("Recording…", self._recording_dialog),
                stretch=False,
            ),
        )
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setToolTip("Title of the X11 window that receives the keys")
        form.addRow(
            "Keys go to", _row(self.target, _button("Refresh", self.refresh_windows), stretch=False)
        )
        self.source.setMinimumWidth(320)
        self.target.setMinimumWidth(320)
        return box

    def _sounds_box(self) -> QGroupBox:
        box = QGroupBox("Sounds (events of the detector library)")
        layout = QVBoxLayout(box)
        self.sounds = QTableWidget(0, 5)
        self.sounds.setHorizontalHeaderLabels(
            ["Sound", "Clips", "Threshold", "Min level (dBFS)", "Live: best / level"]
        )
        self.sounds.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.sounds.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sounds.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sounds.verticalHeader().setVisible(False)
        self.sounds.setMinimumHeight(110)
        layout.addWidget(self.sounds)
        self.new_sound = QComboBox()
        self.new_sound.setEditable(True)
        self.new_sound.setMinimumWidth(140)
        layout.addLayout(
            _row(
                self.new_sound,
                _button("Add sound", lambda: self.add_sound(self.new_sound.currentText())),
                _button("Remove", self.remove_sound),
                _button("Add clip from WAV…", self._clip_dialog),
                _button("Save to library", self.save_sound_to_library),
            )
        )
        hint = QLabel(
            "Threshold and min level differing from the library are saved in the profile; "
            "“Save to library” makes them the library's own."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return box

    def _rules_box(self) -> QGroupBox:
        box = QGroupBox("Rules (steps: a key name such as 9 or f, or wait SECONDS)")
        form = QFormLayout(box)
        self.on_start = QLineEdit()
        self.on_start.setPlaceholderText("e.g. 9")
        form.addRow("On start", self.on_start)
        self.rules = QTableWidget(0, 2)
        self.rules.setHorizontalHeaderLabels(["Sound", "Actions"])
        self.rules.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rules.verticalHeader().setVisible(False)
        self.rules.setMinimumHeight(90)
        form.addRow("On sound", self.rules)
        form.addRow(
            "",
            _row(
                _button("Add rule", lambda: self._add_rule("", "")),
                _button("Remove rule", self._remove_rule),
            ),
        )
        self.silence_on = QCheckBox("after")
        self.silence_seconds = _seconds(3600.0, 1.0)
        self.silence_seconds.setMinimum(0.5)
        self.on_silence = QLineEdit()
        self.on_silence.setPlaceholderText("e.g. 9")
        silence = _row(
            self.silence_on, self.silence_seconds, QLabel("without a trigger:"), stretch=False
        )
        silence.addWidget(self.on_silence, 1)
        form.addRow("On silence", silence)
        return box

    def _stop_box(self) -> QGroupBox:
        box = QGroupBox("Stop and timing")
        form = QFormLayout(box)
        self.max_minutes = QDoubleSpinBox()
        self.max_minutes.setRange(0.0, 24 * 60.0)
        self.max_minutes.setDecimals(1)
        self.max_minutes.setSpecialValueText("off")
        self.max_minutes.setSuffix(" min")
        self.max_minutes.setToolTip("Stop after this many minutes")
        self.max_silent = _spin(10_000, "Stop after this many cycles in a row without a trigger")
        self.max_cycles = _spin(1_000_000, "Stop after this many cycles")
        self.ignore_after = _seconds(60.0, 0.1)
        self.ignore_after.setToolTip("Sounds that start this soon after a key are ignored")
        self.start_delay = _seconds(600.0, 1.0)
        self.start_delay.setToolTip("Wait after switching into the target window")
        form.addRow("Max minutes", self.max_minutes)
        form.addRow("Max silent cycles in a row", self.max_silent)
        form.addRow("Max cycles", self.max_cycles)
        form.addRow("Ignore sounds after a key", self.ignore_after)
        form.addRow("Start delay after switching in", self.start_delay)
        return box

    def _run_panel(self) -> QVBoxLayout:
        panel = QVBoxLayout()
        self.start_button = _button("Start", self.start_run)
        self.stop_button = _button("Stop", self.stop_run)
        self.stop_button.setEnabled(False)
        self.dry_run = QCheckBox("Dry run (log keys, send nothing)")
        self.dry_run.setChecked(True)
        self.record = QCheckBox("Record audio")
        panel.addLayout(_row(self.start_button, self.stop_button, self.dry_run, self.record))
        hint = QLabel(
            "A live run starts when you switch into the target window (Alt+Tab), after the "
            "start delay, and stops when that window loses focus."
        )
        hint.setWordWrap(True)
        panel.addWidget(hint)
        self.counters = QLabel("Not running")
        self.counters.setWordWrap(True)
        panel.addWidget(self.counters)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        panel.addWidget(self.log, 1)
        return panel

    # -- profile <-> widgets -------------------------------------------------------------------

    def set_profile(self, p: Profile, path: Path | None = None) -> None:
        self.path = path
        self.path_label.setText(str(path) if path else "(not saved)")
        if path is not None:
            self.profile_box.setCurrentText(path.stem)
        self.source.setCurrentText(p.source)
        self.target.setCurrentText(p.target)
        self.sounds.setRowCount(0)
        self._hidden_overrides = {}
        for name in p.sound_names():
            self.add_sound(name, p.sounds.get(name, {}))
        self.on_start.setText(profiles.format_actions(p.on_start))
        self.rules.setRowCount(0)
        for rule in p.on_sound:
            self._add_rule(rule.sound, profiles.format_actions(rule.actions))
        silence = p.on_silence
        self.silence_on.setChecked(silence is not None)
        _put(self.silence_seconds, silence.seconds if silence else profiles.DEFAULT_SILENCE)
        self.on_silence.setText(profiles.format_actions(silence.actions) if silence else "")
        _put(self.max_minutes, p.stop.max_minutes)
        _put(self.max_silent, p.stop.max_silent_in_row)
        _put(self.max_cycles, p.stop.max_cycles)
        _put(self.ignore_after, p.ignore_after_action)
        _put(self.start_delay, p.start_delay)

    def profile(self) -> Profile:
        """The profile as edited; raises :class:`ProfileError` for a malformed action list."""
        sounds: dict[str, dict[str, float]] = {}
        for row in range(self.sounds.rowCount()):
            name = self._sound_name(row)
            overrides = dict(self._hidden_overrides.get(name, {}))
            stored = self.library.settings(name)
            for key, col in (("threshold", COL_THRESHOLD), ("min_level_db", COL_MIN_LEVEL)):
                spin = self.sounds.cellWidget(row, col)
                assert isinstance(spin, QDoubleSpinBox)
                if _unchanged(spin):
                    if spin.property("override"):  # keep the profile's own value exactly
                        overrides[key] = spin.property("exact")
                elif round(spin.value(), spin.decimals()) != round(stored[key], spin.decimals()):
                    overrides[key] = spin.value()
            sounds[name] = overrides
        rules = []
        for row in range(self.rules.rowCount()):
            sound = self._cell(self.rules, row, 0)
            if not profiles.NAME_RE.match(sound):
                raise ProfileError(f"rule {row + 1}: invalid sound name {sound!r}")
            actions = profiles.parse_actions(self._cell(self.rules, row, 1))
            rules.append(SoundRule(sound, actions))
        silence = None
        if self.silence_on.isChecked():
            seconds = _get(self.silence_seconds) or profiles.DEFAULT_SILENCE
            silence = SilenceRule(seconds, profiles.parse_actions(self.on_silence.text()))
        p = Profile(
            source=self.source.currentText().strip(),
            target=self.target.currentText().strip(),
            sounds=sounds,
            on_start=profiles.parse_actions(self.on_start.text()),
            on_sound=rules,
            on_silence=silence,
            stop=StopRules(
                max_minutes=_get(self.max_minutes) or None,
                max_silent_in_row=int(_get(self.max_silent) or 0) or None,
                max_cycles=int(_get(self.max_cycles) or 0) or None,
            ),
            ignore_after_action=_get(self.ignore_after) or 0.0,
            start_delay=_get(self.start_delay) or 0.0,
        )
        return profiles.loads(profiles.dumps(p))  # the same checks as a file

    @staticmethod
    def _cell(table: QTableWidget, row: int, col: int) -> str:
        item = table.item(row, col)
        return item.text().strip() if item is not None else ""

    def _sound_name(self, row: int) -> str:
        return self._cell(self.sounds, row, COL_NAME)

    def _add_rule(self, sound: str, actions: str) -> None:
        row = self.rules.rowCount()
        self.rules.insertRow(row)
        self.rules.setItem(row, 0, QTableWidgetItem(sound))
        self.rules.setItem(row, 1, QTableWidgetItem(actions))

    def _remove_rule(self) -> None:
        row = self.rules.currentRow()
        if row >= 0:
            self.rules.removeRow(row)

    # -- profiles on disk ----------------------------------------------------------------------

    def refresh_profiles(self) -> None:
        current = self.profile_box.currentText()
        self.profile_box.clear()
        self.profile_box.addItems(profiles.list_profiles())
        self.profile_box.setCurrentText(current)

    def _profile_chosen(self, index: int) -> None:
        self.load_path(profiles.resolve(self.profile_box.itemText(index)))

    def new_profile(self) -> None:
        self.set_profile(Profile())
        self.append_log("new profile (not saved yet)")

    def load_path(self, path: Path) -> bool:
        try:
            p = profiles.load(path)
        except ProfileError as e:
            self.append_log(f"error: {e}")
            return False
        self.set_profile(p, path)
        self.append_log(f"loaded {path}")
        return True

    def save(self) -> bool:
        """Save into the profiles folder. A profile opened from elsewhere (e.g. ``examples/``)
        is saved as a copy there; the opened file is never overwritten by Save."""
        if self.path is None:
            self._save_as_dialog()
            return self.path is not None
        folder = profiles.profiles_dir()
        if self.path.resolve().parent == folder.resolve():
            return self.save_to(self.path)
        copy = folder / self.path.name
        if copy.exists():
            self.append_log(
                f"not saved: {self.path} is outside the profiles folder and {copy} already "
                "exists; use Save as…"
            )
            return False
        opened = self.path
        if not self.save_to(copy):
            return False
        self.append_log(f"saved as a new profile in {folder}; {opened} was not changed")
        return True

    def save_to(self, path: Path) -> bool:
        try:
            p = self.profile()
            profiles.save(p, path)
        except (ProfileError, ValueError, OSError) as e:
            self.append_log(f"error: not saved: {e}")
            return False
        self.path = path
        self.path_label.setText(str(path))
        self.refresh_profiles()
        self.profile_box.setCurrentText(path.stem)
        self.append_log(f"saved {path}")
        return True

    def _open_dialog(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Open profile", str(profiles.profiles_dir()), "Profiles (*.toml)"
        )
        if name:
            self.load_path(Path(name))

    def _save_as_dialog(self) -> None:
        folder = profiles.profiles_dir()
        folder.mkdir(parents=True, exist_ok=True)
        suggestion = folder / f"{self.path.stem if self.path else 'new-profile'}.toml"
        name, _ = QFileDialog.getSaveFileName(
            self, "Save profile", str(suggestion), "Profiles (*.toml)"
        )
        if name:
            path = Path(name)
            self.save_to(path if path.suffix == ".toml" else path.with_suffix(".toml"))

    # -- source and target ---------------------------------------------------------------------

    def refresh_sources(self) -> None:
        current = self.source.currentText()
        self.source.clear()
        self.source.addItems([*self.list_sources(), "default"])
        self.source.setCurrentText(current)

    def refresh_windows(self) -> None:
        current = self.target.currentText()
        self.target.clear()
        self.target.addItems(self.list_windows())
        self.target.setCurrentText(current)

    def _recording_dialog(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Replay a recording", "", "WAV files (*.wav)")
        if name:
            self.source.setCurrentText(f"file:{name}")
            self.dry_run.setChecked(True)

    # -- sounds --------------------------------------------------------------------------------

    def add_sound(self, name: str, overrides: dict[str, float] | None = None) -> bool:
        name = name.strip()
        if not profiles.NAME_RE.match(name):
            self.append_log(f"error: invalid sound name {name!r}")
            return False
        if any(self._sound_name(r) == name for r in range(self.sounds.rowCount())):
            return False
        overrides = overrides or {}
        stored = self.library.settings(name)
        row = self.sounds.rowCount()
        self.sounds.insertRow(row)
        self.sounds.setItem(row, COL_NAME, QTableWidgetItem(name))
        self.sounds.setItem(row, COL_CLIPS, QTableWidgetItem())
        self.sounds.setItem(row, COL_LIVE, QTableWidgetItem("-"))
        threshold = QDoubleSpinBox()
        threshold.setRange(0.01, 1.0)
        threshold.setDecimals(2)
        threshold.setSingleStep(0.01)
        min_level = QDoubleSpinBox()
        min_level.setRange(-120.0, 0.0)
        min_level.setDecimals(1)
        for key, spin in (("threshold", threshold), ("min_level_db", min_level)):
            _put(spin, overrides.get(key, stored[key]))
            spin.setProperty("override", key in overrides)
        self.sounds.setCellWidget(row, COL_THRESHOLD, threshold)
        self.sounds.setCellWidget(row, COL_MIN_LEVEL, min_level)
        hidden = {k: v for k, v in overrides.items() if k not in SHOWN_SETTINGS}
        if hidden:
            self._hidden_overrides[name] = hidden
        self._update_clips(row)
        return True

    def _update_clips(self, row: int) -> None:
        name = self._sound_name(row)
        try:
            clips = len(self.library.info(name).clips)
            text = str(clips)
        except KeyError:
            text = "0 (not in the library)"
        item = self.sounds.item(row, COL_CLIPS)
        if item is not None:
            item.setText(text)

    def _selected_sound(self) -> tuple[int, str] | None:
        row = self.sounds.currentRow()
        if row < 0:
            if self.sounds.rowCount() != 1:
                self.append_log("select a sound first")
                return None
            row = 0
        return row, self._sound_name(row)

    def remove_sound(self) -> None:
        selected = self._selected_sound()
        if selected is not None:
            self._hidden_overrides.pop(selected[1], None)
            self.sounds.removeRow(selected[0])

    def save_sound_to_library(self) -> None:
        selected = self._selected_sound()
        if selected is None:
            return
        row, name = selected
        spins = {}
        for key, col in (("threshold", COL_THRESHOLD), ("min_level_db", COL_MIN_LEVEL)):
            spin = self.sounds.cellWidget(row, col)
            assert isinstance(spin, QDoubleSpinBox)
            spins[key] = spin
        values = {key: _get(spin) for key, spin in spins.items()}
        try:
            self.library.update_settings(name, values)
        except (ValueError, OSError) as e:
            self.append_log(f"error: not saved to the library: {e}")
            return
        for key, spin in spins.items():  # the library has them now: no profile override
            _put(spin, values[key])
            spin.setProperty("override", False)
        self.append_log(
            f"saved threshold {values['threshold']!r} and min level {values['min_level_db']!r} "
            f"dBFS of {name!r} to the detector library"
        )

    def add_clip_from(self, wav: Path, name: str) -> Path | None:
        """Cut the loudest sound out of ``wav`` (as ``sounddetector add`` does) and store it as a
        clip of ``name`` in the library."""
        try:
            samples, rate = load_wav(wav)
            mono = to_mono(samples)
            start = find_onset(mono, rate)
            clip = trim(mono, rate, start, find_end(mono, rate, start) - start)
            path = self.library.add(name, clip, rate)
        except (ValueError, OSError) as e:
            self.append_log(f"error: clip not added: {e}")
            return None
        self.append_log(f"added clip {path} ({len(clip) / rate:.2f} s from {start:.2f} s of {wav})")
        if not any(self._sound_name(r) == name for r in range(self.sounds.rowCount())):
            self.add_sound(name)
        for row in range(self.sounds.rowCount()):
            self._update_clips(row)
        return path

    def _clip_dialog(self) -> None:
        selected = self._selected_sound()
        name = selected[1] if selected else self.new_sound.currentText().strip()
        if not name:
            return
        wav, _ = QFileDialog.getOpenFileName(
            self, f"Add a clip to {name!r}", "", "WAV files (*.wav)"
        )
        if wav:
            self.add_clip_from(Path(wav), name)

    # -- running -------------------------------------------------------------------------------

    def start_run(self) -> bool:
        if self.run is not None and self.run.engine.running:
            return False
        try:
            p = self.profile()
            run = prepare(
                p,
                name=self.path.stem if self.path else "unsaved",
                dry_run=self.dry_run.isChecked(),
                record=self.record.isChecked(),
                library=self.library,
                on_log=self.bridge.log.emit,
                on_counters=self.bridge.counters.emit,
                on_meter=self.bridge.meter.emit,
                on_finish=self.bridge.finished.emit,
            )
        except (ProfileError, RunError) as e:
            self.append_log(f"error: not started: {e}")
            return False
        except Exception as e:  # e.g. the runs folder is not writable
            self.append_log(f"error: not started: {type(e).__name__}: {e}")
            return False
        self.run = run
        self.last_result = None
        self.append_log(f"run folder {run.folder}")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.counters.setText("Running")
        run.start()
        return True

    def stop_run(self) -> None:
        if self.run is not None:
            self.run.stop("stopped from the window")

    def append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _show_counters(self, c: Counters) -> None:
        triggers = ", ".join(f"{k} {v}" for k, v in c.triggers.items()) or "none"
        self.counters.setText(
            f"Cycles {c.cycles} · keys {c.keys} · triggers: {triggers} · silent {c.silences} "
            f"({c.silent_in_row} in a row) · ignored {c.ignored} · {c.minutes:.1f} min"
        )

    def _show_meter(self, m: Meter) -> None:
        for row in range(self.sounds.rowCount()):
            score = m.peaks.get(self._sound_name(row))
            item = self.sounds.item(row, COL_LIVE)
            if item is not None and score is not None:
                best = f"{score:.2f}" if score > -1 else "-"
                item.setText(f"{best} / {m.level_db:.1f} dBFS")

    def _finished(self, result: Result) -> None:
        self.last_result = result
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._show_counters(result.counters)
        self.counters.setText(f"Stopped: {result.reason}\n{self.counters.text()}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.run is not None and self.run.engine.running:
            self.run.stop("window closed")
            self.run.engine.join(3.0)
        super().closeEvent(event)


def main(profile: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow()
    names = profiles.list_profiles()
    if profile is not None:
        window.load_path(profiles.resolve(profile))
    elif names:
        window.load_path(profiles.resolve(names[0]))
    window.show()
    return app.exec()
