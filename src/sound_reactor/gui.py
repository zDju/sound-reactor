"""The window: the profile on the left, the run on the right.

The run panel is meant to be watched from a second screen: a large coloured status line, the
Start / Try without keys / Stop buttons, what would block a start, counters, a meter per sound
and a short log. Errors are shown in the window, not in pop-ups; dialogs open only for files
and for unsaved changes. The engine runs in its own thread and reaches the window through Qt
signals.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from sound_event_detector import Library, list_apps, load_wav
from sound_event_detector.audio import find_end, find_onset, to_mono, trim
from sound_event_detector.capture import AppStream, find_app

from . import profile as profiles
from . import x11
from .engine import (
    ACTING,
    ARMED,
    CONNECTING,
    REACTING,
    STARTING,
    STOPPED,
    WAITING,
    Counters,
    Meter,
    Result,
    Status,
)
from .profile import Profile, ProfileError, SilenceRule, SoundRule, StopRules
from .runner import Run, RunError, prepare, problems, replay_path

COL_NAME, COL_CLIPS, COL_THRESHOLD, COL_MIN_LEVEL = range(4)
DEFAULT_SIZE = QSize(1150, 760)

# status line: background and text colour per phase (readable on light and dark themes)
COLOURS = {
    None: ("#e3e5e8", "#2b2f33"),
    CONNECTING: ("#fff1c2", "#4d3800"),
    ARMED: ("#ffd970", "#4d3800"),
    STARTING: ("#ffc24d", "#4d2e00"),
    ACTING: ("#c6e2ff", "#00335c"),
    WAITING: ("#c6e2ff", "#00335c"),
    REACTING: ("#9fe2a8", "#0b3d14"),
    STOPPED: ("#e3e5e8", "#2b2f33"),
    "error": ("#f6c1c1", "#5c0b0b"),
}

Answer = str  # "save", "discard" or "cancel"
BLANK = "\u2013"  # shown where there is no value yet


def playing_apps() -> list[AppStream]:
    """Applications playing audio now (PipeWire); empty if they cannot be listed."""
    try:
        return list_apps()
    except Exception:
        return []


def open_in_file_manager(folder: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def duration(seconds: float) -> str:
    """``261.5`` -> ``4:21``; an hour or more -> ``1:02:03``."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def status_text(s: Status | None) -> str:
    """The status line for a run's :class:`Status` (None: no run yet)."""
    if s is None:
        return "Not running"
    if s.phase == CONNECTING:
        text = "Connecting to the audio…"
    elif s.phase == ARMED:
        text = f"Armed: switch into {s.detail} (Alt+Tab)"
    elif s.phase == STARTING:
        text = f"Starting in {math.ceil(s.remaining or 0.0)} s"
    elif s.phase == ACTING:
        text = (
            "Running “on start”" if s.detail == "start" else "Nothing heard: running “on silence”"
        )
    elif s.phase == WAITING:
        text = f"Waiting for a sound ({int(s.elapsed)} s)"
    elif s.phase == REACTING:
        text = f"Reacting: {s.detail}"
    elif s.phase == STOPPED:
        return f"Stopped: {s.detail}"
    else:
        text = s.phase
    return f"{text} · without keys" if s.dry_run else text


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


class _Bridge(QObject):
    """Carries engine callbacks (engine thread) to the window (GUI thread)."""

    log = Signal(str)
    counters = Signal(object)
    meter = Signal(object)
    state = Signal(object)
    finished = Signal(object)


def _button(text: str, slot: Callable[[], object], tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(slot)
    if tip:
        b.setToolTip(tip)
    return b


def _tool(text: str, slot: Callable[[], object], tip: str) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.clicked.connect(slot)
    return b


def _scaled(font: QFont, factor: float, *, bold: bool = False) -> QFont:
    scaled = QFont(font)
    if scaled.pointSizeF() > 0:
        scaled.setPointSizeF(scaled.pointSizeF() * factor)
    elif scaled.pixelSize() > 0:
        scaled.setPixelSize(round(scaled.pixelSize() * factor))
    scaled.setBold(bold)
    return scaled


def _compact(combo: QComboBox, characters: int) -> None:
    """A combo box as wide as ``characters``, not as its longest entry."""
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    combo.setMinimumContentsLength(characters)
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)


def _row(*widgets: QWidget | str, stretch: bool = True) -> QHBoxLayout:
    layout = QHBoxLayout()
    for w in widgets:
        layout.addWidget(QLabel(w) if isinstance(w, str) else w)
    if stretch:
        layout.addStretch(1)
    return layout


def _limit(maximum: int, suffix: str, tip: str) -> QSpinBox:
    s = QSpinBox()
    s.setRange(0, maximum)
    s.setSpecialValueText("off")
    s.setSuffix(suffix)
    s.setToolTip(f"{tip} (0 = off)")
    return s


def _seconds(maximum: float, step: float = 0.5) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(0.0, maximum)
    s.setDecimals(2)
    s.setSingleStep(step)
    s.setSuffix(" s")
    return s


class MeterBar(QWidget):
    """A sound's best similarity in about the last second, with a marker at its threshold;
    green when the threshold is reached."""

    def __init__(self, name: str, threshold: float) -> None:
        super().__init__()
        self.name = name
        self.threshold = threshold
        self.score: float | None = None
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    @property
    def above(self) -> bool:
        return self.score is not None and self.score >= self.threshold

    def set_score(self, score: float | None) -> None:
        self.score = None if score is None or score <= -1 else score
        self.setToolTip(self.text())
        self.update()

    def text(self) -> str:
        best = BLANK if self.score is None else f"{self.score:.2f}"
        return f"{self.name}  {best} / {self.threshold:.2f}"

    def sizeHint(self) -> QSize:
        return QSize(200, 26)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            r = self.rect().adjusted(0, 1, -1, -2)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#9aa3ad"), 1))
            painter.setBrush(QColor("#eef0f2"))
            painter.drawRoundedRect(r, 4, 4)
            if self.score is not None and self.score > 0:
                filled = r.adjusted(1, 1, 0, 0)
                filled.setWidth(round(min(1.0, self.score) * (r.width() - 1)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#2e9e4f" if self.above else "#7b93ad"))
                painter.drawRoundedRect(filled, 3, 3)
            x = r.left() + round(self.threshold * r.width())
            painter.setPen(QPen(QColor("#1d2329"), 2))
            painter.drawLine(x, r.top() - 1, x, r.bottom() + 1)
            painter.setPen(QColor("#111111"))
            text = r.adjusted(8, 0, -8, 0)
            painter.drawText(
                text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.name
            )
            best = BLANK if self.score is None else f"{self.score:.2f}"
            painter.drawText(
                text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, best
            )
        finally:
            painter.end()


class Collapsible(QWidget):
    """A titled section that can be folded away (folded at first)."""

    def __init__(self, title: str, content: QWidget) -> None:
        super().__init__()
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.toggle.toggled.connect(self.set_expanded)
        self.content = content
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle)
        layout.addWidget(content)
        self.set_expanded(False)

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.setChecked(expanded)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)

    @property
    def expanded(self) -> bool:
        return self.toggle.isChecked()


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        library: Library | None = None,
        list_apps: Callable[[], list[AppStream]] = playing_apps,
        list_windows: Callable[[], list[str]] = x11.window_titles,
        settings: QSettings | None = None,
        open_folder: Callable[[Path], object] = open_in_file_manager,
        ask_unsaved: Callable[[str], Answer] | None = None,
    ) -> None:
        super().__init__()
        self.library = library or Library()
        self.list_apps = list_apps
        self.list_windows = list_windows
        self.settings = settings or QSettings("sound-reactor", "gui")
        self.open_folder = open_folder
        self.ask_unsaved = ask_unsaved or self._ask_unsaved_dialog
        self.path: Path | None = None
        self.run: Run | None = None
        self.last_result: Result | None = None
        self._hidden_overrides: dict[str, dict[str, float]] = {}  # not shown in the table
        self._saved: Profile | None = None  # as loaded or last saved: for the * in the title
        self._loading = False
        self._target_auto = False  # the target was filled in from the source
        self._apps: list[AppStream] = []
        self._windows: list[str] = []
        self._start_errors: list[str] = []  # from the last failed start
        self.bridge = _Bridge()
        self.bridge.log.connect(self.append_log)
        self.bridge.counters.connect(self._show_counters)
        self.bridge.meter.connect(self._show_meter)
        self.bridge.state.connect(self._show_status)
        self.bridge.finished.connect(self._finished)
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(250)
        self._check_timer.timeout.connect(self.refresh_checks)

        self._build_menu()
        config = QWidget()
        left = QVBoxLayout(config)
        left.addLayout(self._profile_bar())
        left.addWidget(self._source_box())
        left.addWidget(self._sounds_box())
        left.addWidget(self._rules_box())
        left.addWidget(self._stop_box())
        left.addWidget(self._advanced_box())
        left.addStretch(1)
        self.config_scroll = QScrollArea()
        self.config_scroll.setWidgetResizable(True)
        self.config_scroll.setWidget(config)
        self.config_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.config_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.config_scroll.setMinimumWidth(config.minimumSizeHint().width() + 20)

        run_panel = QWidget()
        run_panel.setLayout(self._run_panel())
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.config_scroll)
        self.splitter.addWidget(run_panel)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 4)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        self.path_label = QLabel()
        self.statusBar().addPermanentWidget(self.path_label)

        geometry = self.settings.value("geometry")
        if geometry is None or not self.restoreGeometry(geometry):  # pyright: ignore
            self.resize(DEFAULT_SIZE)
        splitter = self.settings.value("splitter")
        if splitter is not None:
            self.splitter.restoreState(splitter)  # pyright: ignore[reportArgumentType]

        self.refresh_sources()
        self.refresh_windows()
        self.refresh_profiles()
        self.set_profile(Profile())

    # -- layout --------------------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&Profile")
        self.actions_by_name: dict[str, QAction] = {}
        for name, text, shortcut, slot in (
            ("new", "&New", QKeySequence.StandardKey.New, self.new_profile),
            ("open", "&Open…", QKeySequence.StandardKey.Open, self._open_dialog),
            ("save", "&Save", QKeySequence.StandardKey.Save, self.save),
            ("save_as", "Save &as…", QKeySequence.StandardKey.SaveAs, self._save_as_dialog),
            (None, "", None, None),
            ("replay", "&Replay a recording…", None, self._recording_dialog),
            (None, "", None, None),
            ("quit", "&Quit", QKeySequence.StandardKey.Quit, self.close),
        ):
            if name is None:
                menu.addSeparator()
                continue
            action = menu.addAction(text)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            self.actions_by_name[name] = action
        self.actions_by_name["replay"].setToolTip(
            "Use a WAV recording as the source and try the profile on it without keys"
        )

    def _profile_bar(self) -> QHBoxLayout:
        self.profile_box = QComboBox()
        _compact(self.profile_box, 12)
        self.profile_box.activated.connect(self._profile_chosen)
        self.save_button = _button("Save", self.save, "Save the profile (Ctrl+S)")
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Profile"))
        bar.addWidget(self.profile_box, 1)
        bar.addWidget(self.save_button)
        return bar

    def _source_box(self) -> QGroupBox:
        box = QGroupBox("Sound and window")
        form = QFormLayout(box)
        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.setToolTip("app:NAME (one application), device:TEXT, default, or file:PATH.wav")
        self.source.editTextChanged.connect(self._changed)
        self.source.activated.connect(self._source_picked)
        source_edit = self.source.lineEdit()
        assert source_edit is not None
        source_edit.editingFinished.connect(self._source_picked)
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setToolTip("Title of the X11 window that receives the keys")
        self.target.editTextChanged.connect(self._changed)
        self.target.activated.connect(self._target_chosen)
        line_edit = self.target.lineEdit()
        assert line_edit is not None
        line_edit.textEdited.connect(self._target_chosen)
        for combo in (self.source, self.target):
            _compact(combo, 16)
        form.addRow(
            "Listen to",
            _row(self.source, _tool("↻", self.refresh_sources, "Refresh the list"), stretch=False),
        )
        form.addRow(
            "Keys go to",
            _row(self.target, _tool("↻", self.refresh_windows, "Refresh the list"), stretch=False),
        )
        return box

    def _sounds_box(self) -> QGroupBox:
        box = QGroupBox("Sounds")
        layout = QVBoxLayout(box)
        self.sounds = QTableWidget(0, 4)
        self.sounds.setHorizontalHeaderLabels(["Sound", "Clips", "Threshold", "Min level (dBFS)"])
        self.sounds.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.sounds.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sounds.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sounds.verticalHeader().setVisible(False)
        self.sounds.setMinimumHeight(90)
        self.sounds.setToolTip(
            "Sounds are events of the detector library. Threshold and min level changed here "
            "are saved in this profile; otherwise the library's values apply."
        )
        layout.addWidget(self.sounds)
        self.new_sound = QComboBox()
        self.new_sound.setEditable(True)
        _compact(self.new_sound, 8)
        self.new_sound.setToolTip("A sound from the detector library, or a new name")
        layout.addLayout(
            _row(
                self.new_sound,
                _button("Add", lambda: self.add_sound(self.new_sound.currentText())),
                _button("Remove", self.remove_sound),
                _button(
                    "Add clip from WAV…",
                    self._clip_dialog,
                    "Cut the sound out of a WAV file and add it to the library as a clip",
                ),
            )
        )
        return box

    def _rules_box(self) -> QGroupBox:
        box = QGroupBox("Rules (steps: a key such as 9 or f, or wait SECONDS)")
        form = QFormLayout(box)
        self.on_start = QLineEdit()
        self.on_start.setPlaceholderText("e.g. 9")
        self.on_start.textChanged.connect(self._changed)
        form.addRow("On start", self.on_start)
        self.rules = QTableWidget(0, 2)
        self.rules.setHorizontalHeaderLabels(["Sound", "Actions"])
        self.rules.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rules.verticalHeader().setVisible(False)
        self.rules.setMinimumHeight(80)
        self.rules.itemChanged.connect(self._changed)
        form.addRow("On sound", self.rules)
        form.addRow(
            "",
            _row(
                _button("Add rule", lambda: self._add_rule("", "")),
                _button("Remove rule", self._remove_rule),
            ),
        )
        self.silence_on = QCheckBox("after")
        self.silence_on.toggled.connect(self._changed)
        self.silence_seconds = _seconds(3600.0, 1.0)
        self.silence_seconds.setMinimum(0.5)
        self.silence_seconds.valueChanged.connect(self._changed)
        self.on_silence = QLineEdit()
        self.on_silence.setPlaceholderText("e.g. 9")
        self.on_silence.textChanged.connect(self._changed)
        silence = _row(self.silence_on, self.silence_seconds, "with no sound:", stretch=False)
        silence.addWidget(self.on_silence, 1)
        form.addRow("On silence", silence)
        return box

    def _stop_box(self) -> QGroupBox:
        box = QGroupBox("Stop")
        self.max_minutes = QDoubleSpinBox()
        self.max_minutes.setRange(0.0, 24 * 60.0)
        self.max_minutes.setDecimals(1)
        self.max_minutes.setSpecialValueText("off")
        self.max_minutes.setSuffix(" min")
        self.max_minutes.setToolTip("Stop after this many minutes (0 = off)")
        self.max_cycles = _limit(1_000_000, " cycles", "Stop after this many cycles")
        self.max_silent = _limit(10_000, "", "Stop after this many cycles in a row without a sound")
        for spin in (self.max_minutes, self.max_cycles, self.max_silent):
            spin.valueChanged.connect(self._changed)
        line = _row(
            "Stop after",
            self.max_minutes,
            "·",
            self.max_cycles,
            "·",
            self.max_silent,
            "silent in a row",
        )
        line.setSpacing(4)
        box.setLayout(line)
        return box

    def _advanced_box(self) -> Collapsible:
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(18, 0, 0, 0)
        self.ignore_after = _seconds(60.0, 0.1)
        self.ignore_after.setToolTip("Sounds that start this soon after a key are ignored")
        self.start_delay = _seconds(600.0, 1.0)
        self.start_delay.setToolTip(
            "Wait after switching into the window before the first key (at least 5 s for Start)"
        )
        for spin in (self.ignore_after, self.start_delay):
            spin.valueChanged.connect(self._changed)
        self.record = QCheckBox("Record the audio of each run (into its run folder)")
        form.addRow("Ignore sounds after a key", self.ignore_after)
        form.addRow("Start delay after switching in", self.start_delay)
        form.addRow(self.record)
        self.advanced = Collapsible("Advanced", content)
        return self.advanced

    def _run_panel(self) -> QVBoxLayout:
        panel = QVBoxLayout()
        self.status_line = QLabel()
        self.status_line.setFont(_scaled(self.status_line.font(), 1.6, bold=True))
        self.status_line.setWordWrap(True)
        self.status_line.setMinimumHeight(56)
        self.status_line.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        panel.addWidget(self.status_line)
        self._show_status(None)

        self.start_button = _button(
            "Start",
            lambda: self.start_run(live=True),
            "Arm the run: it starts when you switch into the window (Alt+Tab) and stops when "
            "that window loses focus",
        )
        self.try_button = _button(
            "Try without keys",
            lambda: self.start_run(live=False),
            "Run everything but the keys (logged instead); no focus needed",
        )
        self.stop_button = _button("Stop", self.stop_run)
        self.stop_button.setEnabled(False)
        for b in (self.start_button, self.try_button, self.stop_button):
            b.setMinimumHeight(34)
        panel.addLayout(_row(self.start_button, self.try_button, self.stop_button))
        self.problems_label = QLabel()  # what blocks both buttons, or "Ready to start."
        self.start_only_label = QLabel()  # what blocks only Start: Try without keys still works
        for label in (self.problems_label, self.start_only_label):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            panel.addWidget(label)
        self.start_only_label.setStyleSheet("color: #6b7178;")

        self.summary_box = QFrame()
        self.summary_box.setFrameShape(QFrame.Shape.StyledPanel)
        summary = QHBoxLayout(self.summary_box)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.open_folder_button = _button("Open run folder", self._open_run_folder)
        summary.addWidget(self.summary, 1)
        summary.addWidget(self.open_folder_button)
        self.summary_box.setVisible(False)
        panel.addWidget(self.summary_box)

        counters = QGroupBox("Counters")
        grid = QGridLayout(counters)
        self.counter_values: dict[str, QLabel] = {}
        for row, (key, title) in enumerate(
            (
                ("cycles", "Cycles"),
                ("triggers", "Triggers"),
                ("silent", "Silent cycles"),
                ("hit_rate", "Hit rate"),
                ("elapsed", "Elapsed"),
                ("last", "Last trigger"),
            )
        ):
            value = QLabel(BLANK)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(QLabel(title), row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
            self.counter_values[key] = value
        grid.setColumnStretch(1, 1)
        panel.addWidget(counters)

        meter = QGroupBox("Meter: best similarity per sound, marker at its threshold")
        self.meter_layout = QVBoxLayout(meter)
        self.meter_bars: dict[str, MeterBar] = {}
        self.level_label = QLabel(f"Level {BLANK}")
        self.meter_layout.addWidget(self.level_label)
        panel.addWidget(meter)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setMinimumHeight(90)
        self.log.setFont(_scaled(self.log.font(), 0.9))
        self._log_follow, self._log_scroll_due, self._log_scrolling = True, False, False
        self.log.verticalScrollBar().valueChanged.connect(self._log_scrolled)
        panel.addWidget(self.log, 1)
        return panel

    # -- profile <-> widgets -------------------------------------------------------------------

    def set_profile(self, p: Profile, path: Path | None = None) -> None:
        self._loading = True
        try:
            self.path = path
            self._show_profile_in_box()
            self.source.setCurrentText(p.source)
            self.target.setCurrentText(p.target)
            self._target_auto = False
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
        finally:
            self._loading = False
        self._saved = self._try_profile()
        self._start_errors = []
        self.refresh_checks()

    def profile(self) -> Profile:
        """The profile as edited; raises :class:`ProfileError` for a malformed value."""
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

    def _try_profile(self) -> Profile | None:
        try:
            return self.profile()
        except (ProfileError, ValueError):
            return None

    @property
    def dirty(self) -> bool:
        """The profile differs from the one loaded or last saved."""
        current = self._try_profile()
        return current is None or current != self._saved

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
        self._changed()

    def _remove_rule(self) -> None:
        row = self.rules.currentRow()
        if row >= 0:
            self.rules.removeRow(row)
            self._changed()

    # -- changes and checks --------------------------------------------------------------------

    def _changed(self, *_: object) -> None:
        if not self._loading:
            self._start_errors = []
            self._check_timer.start()

    def refresh_checks(self) -> None:
        """Bring the title (* for unsaved changes), the problems and the meter up to date."""
        self._check_timer.stop()
        name = self.path.stem if self.path else "new profile"
        self.setWindowTitle(f"{name}{'*' if self.dirty else ''} — sound-reactor")
        where = str(self.path) if self.path else "not saved yet"
        self.path_label.setText(where)
        self.profile_box.setToolTip(where)
        self.show_problems()
        if self.run is None or not self.run.engine.running:
            self._build_meter()

    def problems(self) -> tuple[list[str], list[str]]:
        """What blocks Start, and what blocks Try without keys (from the lists last fetched)."""
        try:
            p = self.profile()
        except (ProfileError, ValueError) as e:
            return [str(e)], [str(e)]
        live, dry = problems(p, dry_run=False), problems(p, dry_run=True)
        known = set(self.library.names())
        for name in p.sound_names():
            if name not in known:
                message = f"the sound {name!r} has no clips in the detector library"
                live.append(message)
                dry.append(message)
        kind, _, app = p.source.partition(":")
        app = app.strip()
        if kind == "app" and app and find_app(app, self._apps) is None:  # as capture does
            message = f"nothing called {app!r} is playing audio now (↻ to look again)"
            live.append(message)
            dry.append(message)
        if p.target and replay_path(p.source) is None and p.target not in self._windows:
            live.append(f"no window titled {p.target!r} is open (↻ to look again)")
        return live, dry

    def show_problems(self) -> None:
        """Red: what blocks both buttons (or the last start that failed). Grey: what blocks
        only Start, while Try without keys would work."""
        live, dry = self.problems()
        blocking = self._start_errors + dry
        start_only = [m for m in live if m not in dry]
        if blocking:
            self.problems_label.setText("\n".join(f"• {m}" for m in blocking))
            self.problems_label.setStyleSheet("color: #b3261e;")
        else:
            self.problems_label.setText(
                "Ready to try without keys." if start_only else "Ready to start."
            )
            self.problems_label.setStyleSheet("color: #2e7d32;")
        self.start_only_label.setText("Start only: " + "; ".join(start_only) if start_only else "")
        self.start_only_label.setVisible(bool(start_only))

    # -- profiles on disk ----------------------------------------------------------------------

    def refresh_profiles(self) -> None:
        self.profile_box.clear()
        self.profile_box.addItems(profiles.list_profiles())
        self._show_profile_in_box()

    def _show_profile_in_box(self) -> None:
        """The dropdown shows the loaded profile, or nothing when it is not one of the list."""
        index = -1
        if (
            self.path is not None
            and self.path.parent.resolve() == profiles.profiles_dir().resolve()
        ):
            index = self.profile_box.findText(self.path.stem)
        self.profile_box.setCurrentIndex(index)

    def _profile_chosen(self, index: int) -> None:
        path = profiles.resolve(self.profile_box.itemText(index))
        if path == self.path:
            return
        if not (self.keep_or_discard_changes() and self.load_path(path)):
            self._show_profile_in_box()

    def keep_or_discard_changes(self) -> bool:
        """True when it is fine to replace the profile being edited (asks if it has changes)."""
        if not self.dirty:
            return True
        answer = self.ask_unsaved(self.path.stem if self.path else "the new profile")
        if answer == "save":
            return self.save()
        return answer == "discard"

    def _ask_unsaved_dialog(self, name: str) -> Answer:
        button = QMessageBox.question(
            self,
            "Unsaved changes",
            f"Save the changes to {name}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if button == QMessageBox.StandardButton.Save:
            return "save"
        if button == QMessageBox.StandardButton.Discard:
            return "discard"
        return "cancel"

    def new_profile(self) -> None:
        if self.keep_or_discard_changes():
            self.set_profile(Profile())
            self.append_log("new profile (not saved yet)")

    def load_path(self, path: Path) -> bool:
        try:
            p = profiles.load(path)
        except ProfileError as e:
            self.append_log(f"error: {e}")
            return False
        self.set_profile(p, path)
        self.settings.setValue("last_profile", str(path.resolve()))
        self.append_log(f"loaded {path}")
        return True

    def load_initial(self, profile: str | None = None) -> None:
        """At start: the given profile (an error is logged if it cannot be read), else the last
        one used, else the first saved one; each tried once."""
        if profile is not None:
            self.load_path(profiles.resolve(profile))
            return
        last = self.settings.value("last_profile")
        candidates = [Path(str(last))] if last else []
        candidates += [profiles.resolve(name) for name in profiles.list_profiles()]
        tried: set[Path] = set()
        for path in candidates:
            if path.resolve() in tried or not path.is_file():
                continue
            tried.add(path.resolve())
            if self.load_path(path):
                return

    def save(self) -> bool:
        """Save into the profiles folder. A profile opened from elsewhere (e.g. ``examples/``)
        is saved as a copy there; the opened file is never overwritten by Save."""
        if self.path is None:
            self._save_as_dialog()
            return self.path is not None and not self.dirty
        folder = profiles.profiles_dir()
        if self.path.resolve().parent == folder.resolve():
            return self.save_to(self.path)
        copy = folder / self.path.name
        if copy.exists():
            self.append_log(
                f"not saved: {self.path} is outside the profiles folder and {copy} already "
                "exists; use Profile > Save as…"
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
            self.statusBar().showMessage("Not saved: see the log", 5000)
            return False
        self.path = path
        self._saved = p
        self.settings.setValue("last_profile", str(path.resolve()))
        self.refresh_profiles()
        self.append_log(f"saved {path}")
        self.statusBar().showMessage(f"Saved {path.name}", 3000)
        self.refresh_checks()
        return True

    def _open_dialog(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Open profile", str(profiles.profiles_dir()), "Profiles (*.toml)"
        )
        if name:
            self.open_profile(Path(name))

    def open_profile(self, path: Path) -> bool:
        """Open another profile, asking first if the current one has unsaved changes."""
        return self.keep_or_discard_changes() and self.load_path(path)

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
        self._apps = list(self.list_apps())
        current = self.source.currentText()
        loading, self._loading = self._loading, True
        try:
            self.source.clear()
            self.source.addItems([*sorted({f"app:{s.name}" for s in self._apps}), "default"])
            self.source.setCurrentText(current)
        finally:
            self._loading = loading
        self._changed()

    def refresh_windows(self) -> None:
        self._windows = list(self.list_windows())
        current = self.target.currentText()
        loading, self._loading = self._loading, True
        try:
            self.target.clear()
            self.target.addItems(self._windows)
            self.target.setCurrentText(current)
        finally:
            self._loading = loading
        self._changed()

    def window_for(self, source: str) -> str | None:
        """The open window titled exactly like the application of an ``app:`` source (its name
        as typed, or the name of the application that name finds)."""
        kind, _, app = source.partition(":")
        app = app.strip()
        if kind != "app" or not app:
            return None
        names = {app.lower()}
        stream = find_app(app, self._apps)
        if stream is not None:
            names.add(stream.name.lower())
        return next((title for title in self._windows if title.strip().lower() in names), None)

    def _source_picked(self, *_: object) -> None:
        """A source was picked from the list or typed in full: fill in its window, unless the
        user chose the window; clear a window filled in earlier if none matches now."""
        if self._loading or (self.target.currentText().strip() and not self._target_auto):
            return
        title = self.window_for(self.source.currentText())
        if title is not None:
            self.target.setCurrentText(title)
            self._target_auto = True
        elif self._target_auto:
            self.target.setCurrentText("")
            self._target_auto = False

    def _target_chosen(self, *_: object) -> None:
        self._target_auto = False  # the user picked or typed it: leave it alone

    def _recording_dialog(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Replay a recording", "", "WAV files (*.wav)")
        if name:
            self.source.setCurrentText(f"file:{name}")
            self.statusBar().showMessage("A recording can be tried without keys only", 5000)

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
            spin.setToolTip(f"Library value {stored[key]:g}; a change is saved in this profile")
            spin.valueChanged.connect(self._changed)
        self.sounds.setCellWidget(row, COL_THRESHOLD, threshold)
        self.sounds.setCellWidget(row, COL_MIN_LEVEL, min_level)
        hidden = {k: v for k, v in overrides.items() if k not in ("threshold", "min_level_db")}
        if hidden:
            self._hidden_overrides[name] = hidden
        self._update_clips(row)
        self._changed()
        return True

    def _update_clips(self, row: int) -> None:
        name = self._sound_name(row)
        try:
            text = str(len(self.library.info(name).clips))
        except KeyError:
            text = "0 (none in the library)"
        item = self.sounds.item(row, COL_CLIPS)
        if item is not None:
            item.setText(text)

    def _selected_sound(self) -> tuple[int, str] | None:
        row = self.sounds.currentRow()
        if row < 0:
            if self.sounds.rowCount() != 1:
                self.statusBar().showMessage("Select a sound first", 4000)
                return None
            row = 0
        return row, self._sound_name(row)

    def remove_sound(self) -> None:
        selected = self._selected_sound()
        if selected is not None:
            self._hidden_overrides.pop(selected[1], None)
            self.sounds.removeRow(selected[0])
            self._changed()

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
        self._changed()
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

    def start_run(self, *, live: bool) -> bool:
        """Start (live: keys are sent) or Try without keys (``live=False``)."""
        if self.run is not None and self.run.engine.running:
            return False
        self.refresh_sources()
        self.refresh_windows()
        self._start_errors = []
        live_problems, dry_problems = self.problems()
        if live_problems if live else dry_problems:
            self.show_problems()
            self.statusBar().showMessage("Not started: see the problems under the buttons", 5000)
            return False
        try:
            p = self.profile()
            run = prepare(
                p,
                name=self.path.stem if self.path else "unsaved",
                dry_run=not live,
                record=self.record.isChecked(),
                library=self.library,
                on_log=self.bridge.log.emit,
                on_counters=self.bridge.counters.emit,
                on_meter=self.bridge.meter.emit,
                on_state=self.bridge.state.emit,
                on_finish=self.bridge.finished.emit,
            )
        except (ProfileError, RunError) as e:
            self._start_errors = [str(e)]
        except Exception as e:  # e.g. the runs folder is not writable
            self._start_errors = [f"{type(e).__name__}: {e}"]
        else:
            self.run = run
            self.last_result = None
            self.summary_box.setVisible(False)
            self._show_counters(Counters(triggers={r.sound: 0 for r in p.on_sound}))
            self._build_meter(p)
            self.append_log(f"run folder {run.folder}")
            self._set_running(True)
            run.start()
            self.show_problems()
            return True
        self.show_problems()
        self.append_log(f"error: not started: {self._start_errors[0]}")
        return False

    def stop_run(self) -> None:
        if self.run is not None:
            self.run.stop("stopped from the window")

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.try_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def append_log(self, line: str) -> None:
        """Add a line; the view follows new lines unless the user has scrolled up.

        QPlainTextEdit follows on its own only while its layout keeps up: a replay sends lines
        in bursts, and with appendPlainText alone the view of the wow-session-4 replay ended
        44 lines short of the end. So the end is scrolled to after each burst is laid out.
        """
        self.log.appendPlainText(line)
        if self._log_follow and not self._log_scroll_due:
            self._log_scroll_due = True
            QTimer.singleShot(0, self._scroll_log_to_end)  # after the new lines are laid out

    def _scroll_log_to_end(self) -> None:
        self._log_scroll_due = False
        if not self._log_follow:  # the user scrolled up after this was scheduled
            return
        bar = self.log.verticalScrollBar()
        self._log_scrolling = True
        bar.setValue(bar.maximum())
        self._log_scrolling = False

    def _log_scrolled(self, value: int) -> None:
        if not self._log_scrolling:  # the user: follow again once back at the end
            self._log_follow = value >= self.log.verticalScrollBar().maximum() - 1

    def _show_status(self, s: Status | None) -> None:
        if s is not None:
            self.counter_values["elapsed"].setText(duration(s.run_seconds))
        key = "error" if s is not None and s.phase == STOPPED and s.error else (s and s.phase)
        background, foreground = COLOURS.get(key, COLOURS[None])
        self.status_line.setText(status_text(s))
        self.status_line.setStyleSheet(
            f"QLabel {{ background: {background}; color: {foreground}; border-radius: 6px; "
            "padding: 8px 12px; }"
        )

    def _show_counters(self, c: Counters) -> None:
        triggered = sum(c.triggers.values())
        v = self.counter_values
        v["cycles"].setText(str(c.cycles))
        v["triggers"].setText(", ".join(f"{k} {n}" for k, n in c.triggers.items()) or BLANK)
        v["silent"].setText(f"{c.silences} ({c.silent_in_row} in a row)")
        v["hit_rate"].setText(f"{100.0 * triggered / c.cycles:.0f} %" if c.cycles else BLANK)
        last = c.last_trigger
        v["last"].setText(
            BLANK
            if last is None
            else f"{last.sound} · {last.into_cycle:.1f} s into the cycle · similarity "
            f"{last.similarity:.2f} · {last.level_db:.1f} dBFS"
        )

    def _build_meter(self, p: Profile | None = None) -> None:
        """One bar per sound, with each sound's threshold (as edited, or as run)."""
        thresholds: dict[str, float] = {}
        if p is None:
            for row in range(self.sounds.rowCount()):
                spin = self.sounds.cellWidget(row, COL_THRESHOLD)
                if isinstance(spin, QDoubleSpinBox):
                    thresholds[self._sound_name(row)] = spin.value()
        else:
            for name in p.sound_names():
                stored = self.library.settings(name)["threshold"]
                thresholds[name] = p.sounds.get(name, {}).get("threshold", stored)
        current = {name: bar.threshold for name, bar in self.meter_bars.items()}
        if current == thresholds:
            return
        for bar in self.meter_bars.values():
            self.meter_layout.removeWidget(bar)
            bar.deleteLater()
        self.meter_bars = {}
        for i, (name, threshold) in enumerate(thresholds.items()):
            bar = MeterBar(name, threshold)
            self.meter_layout.insertWidget(i, bar)
            self.meter_bars[name] = bar

    def _show_meter(self, m: Meter) -> None:
        for name, bar in self.meter_bars.items():
            bar.set_score(m.peaks.get(name))
        self.level_label.setText(f"Level {m.level_db:.1f} dBFS")

    def _finished(self, result: Result) -> None:
        self.last_result = result
        self._set_running(False)
        self._show_counters(result.counters)
        c = result.counters
        triggered = sum(c.triggers.values())
        per_sound = ", ".join(f"{k} {n}" for k, n in c.triggers.items())
        rate = f", hit rate {100.0 * triggered / c.cycles:.0f} %" if c.cycles else ""
        how = " (tried without keys)" if self.run is not None and self.run.engine.dry_run else ""
        self.summary.setText(
            f"{'Stopped with a problem' if result.error else 'Stopped'}: {result.reason}{how}.\n"
            f"{c.cycles} cycles, {triggered} triggers"
            f"{f' ({per_sound})' if per_sound else ''}, {c.silences} silent{rate}; "
            f"{duration(c.minutes * 60.0)} long."
        )
        self.summary_box.setVisible(True)
        self.open_folder_button.setEnabled(self.run is not None)
        self.refresh_checks()

    def _open_run_folder(self) -> None:
        if self.run is not None:
            self.open_folder(self.run.folder)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Stop a run first (it must not go on while a question is open), then ask about
        unsaved changes; cancelling keeps the window, with the run stopped."""
        if self.run is not None and self.run.engine.running:
            self.run.stop("the window is closing")
            self.run.engine.join(10.0)
        if not self.keep_or_discard_changes():
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.settings.sync()
        super().closeEvent(event)


def main(profile: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow()
    window.load_initial(profile)
    window.show()
    return app.exec()
