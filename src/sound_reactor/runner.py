"""Turning a profile into a run: sounds from the detector library, the audio source, the target
window (or nothing, in a dry run), a run folder, and the engine. Shared by the CLI and the GUI.

Each run gets a folder outside the repository (``~/.local/state/sound-reactor/runs/`` on Linux,
``%LOCALAPPDATA%\\sound-reactor\\runs`` on Windows, see :mod:`.paths`) holding ``log.txt``,
``counters.json``, a copy of the profile and, if asked for, the recorded source audio.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from sound_event_detector import DEFAULT_PARAMS, FileSource, Library, SoundEvent, open_source
from sound_event_detector.capture import Source

from . import desktop, paths
from . import profile as profiles
from .desktop import Backend, Target
from .engine import (
    MIN_LIVE_START_DELAY,
    Clock,
    Counters,
    Engine,
    Keyboard,
    Listener,
    Meter,
    Result,
    Status,
    SystemClock,
)
from .listen import LiveListener, Recorder, ReplayListener
from .profile import Profile

SAMPLE_RATE = DEFAULT_PARAMS.sample_rate


class RunError(Exception):
    """The profile cannot be run as it is (the message says why)."""


def state_home() -> Path:
    return paths.state_home()


def runs_dir() -> Path:
    return paths.runs_dir()


def replay_path(source: str) -> Path | None:
    """The WAV file of a ``file:`` source, else None."""
    kind, sep, value = source.partition(":")
    return Path(value).expanduser() if sep and kind == "file" else None


def problems(p: Profile, *, dry_run: bool, backend: Backend | None = None) -> list[str]:
    """What prevents ``p`` from running (empty: nothing found). Key names are checked against
    ``backend`` (this platform's by default)."""
    found = []
    if not p.source.strip():
        found.append("the profile has no source")
    replay = replay_path(p.source)
    if replay is not None:
        if not dry_run:
            found.append("a file: source is a replay of a recording; run it as a dry run")
        if not replay.is_file():
            found.append(f"recording not found: {replay}")
    if not dry_run and not p.target.strip():
        found.append("the profile has no target window")
    if not dry_run and p.start_delay < MIN_LIVE_START_DELAY:
        found.append(f"start_delay must be at least {MIN_LIVE_START_DELAY:g} s for a live run")
    if not p.sound_names():
        found.append("the profile has no sounds")
    key_error = (backend or desktop.current()).key_error
    found += [error for key in p.key_names() if (error := key_error(key)) is not None]
    return found


def load_events(p: Profile, library: Library) -> list[SoundEvent]:
    events = []
    for name in p.sound_names():
        try:
            events.append(library.load(name, **p.sounds.get(name, {})))
        except (KeyError, ValueError) as e:
            raise RunError(str(e).strip("'\"")) from e
    return events


def new_run_folder(root: Path, name: str) -> Path:
    stem = f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', name) or 'run'}"
    folder, n = root / stem, 2
    while folder.exists():
        folder, n = root / f"{stem}-{n}", n + 1
    folder.mkdir(parents=True)
    return folder


class NoKeyboard:
    """The keyboard of a dry run: nothing is ever sent."""

    def held(self) -> str | None:
        return None

    def press(self, key: str) -> None:
        raise RuntimeError("a dry run must not send keys")


@dataclass
class Run:
    engine: Engine
    folder: Path
    recording: Path | None

    def start(self) -> None:
        self.engine.start()

    def stop(self, reason: str = "stopped") -> None:
        self.engine.stop(reason)


def prepare(
    p: Profile,
    *,
    name: str = "profile",
    dry_run: bool = False,
    record: bool | str | Path = False,
    runs_root: Path | None = None,
    library: Library | None = None,
    on_log: Callable[[str], object] | None = None,
    on_counters: Callable[[Counters], object] | None = None,
    on_meter: Callable[[Meter], object] | None = None,
    on_state: Callable[[Status], object] | None = None,
    on_finish: Callable[[Result], object] | None = None,
) -> Run:
    """Check ``p``, open what it needs and return a run that has not started yet.

    ``record`` is True (record into the run folder), a WAV path that must not exist yet, or
    False. Raises :class:`RunError` when the profile cannot run; anything opened is closed again
    if preparing fails.
    """
    backend = desktop.current()
    found = problems(p, dry_run=dry_run, backend=backend)
    recording = None if record is True or not record else Path(record).expanduser()
    if recording is not None and recording.exists():
        found.append(f"{recording} already exists; recordings never overwrite a file")
    if found:
        raise RunError("; ".join(found))
    events = load_events(p, library or Library())

    target: Target | None = None
    if not dry_run:
        try:
            target = backend.find_target(p.target)
        except LookupError as e:
            raise RunError(str(e)) from e
        except Exception as e:  # no X server, refused connection
            raise RunError(f"cannot reach {backend.reach}: {type(e).__name__}: {e}") from e
    log_file = None
    try:
        if target is not None:
            bad = []
            for key in p.key_names():
                try:
                    target.keycode(key)
                except ValueError as e:
                    bad.append(str(e))
            bad += backend.target_problems(target)  # e.g. a game running as administrator
            if bad:
                raise RunError("; ".join(bad))

        folder = new_run_folder(runs_root or runs_dir(), name)
        profiles.save(p, folder / "profile.toml")
        if record is True:
            recording = folder / "audio.wav"
        log_file = (folder / "log.txt").open("a", encoding="utf-8")
        run = _assemble(
            p, events, target, folder, recording, log_file, dry_run, backend,
            on_log, on_counters, on_meter, on_state, on_finish,
        )  # fmt: skip
    except BaseException:
        if target is not None:
            target.close()
        if log_file is not None:
            log_file.close()
        raise
    return run


def _assemble(
    p: Profile,
    events: list[SoundEvent],
    target: Target | None,
    folder: Path,
    recording: Path | None,
    log_file: TextIO,
    dry_run: bool,
    backend: Backend,
    on_log: Callable[[str], object] | None,
    on_counters: Callable[[Counters], object] | None,
    on_meter: Callable[[Meter], object] | None,
    on_state: Callable[[Status], object] | None,
    on_finish: Callable[[Result], object] | None,
) -> Run:
    lock = threading.Lock()

    def log(line: str) -> None:
        with lock:
            if not log_file.closed:
                log_file.write(line + "\n")
                log_file.flush()
        if on_log is not None:
            on_log(line)

    replay = replay_path(p.source)
    source: Source
    if replay is not None:
        source = FileSource(replay, sample_rate=SAMPLE_RATE, realtime=False)
    else:
        source = open_source(p.source, sample_rate=SAMPLE_RATE)
    recorder = Recorder(source, recording) if recording is not None else None
    if recorder is not None:
        source = recorder

    listener: Listener
    clock: Clock
    if replay is not None:
        replayer = ReplayListener(events, source)
        listener, clock = replayer, replayer.clock
    else:
        clock = SystemClock()
        listener = LiveListener(events, source, clock, on_log=log)

    def write_counters(c: Counters, result: Result | None = None) -> None:
        data = asdict(c)
        if result is not None:
            data |= {"stop_reason": result.reason, "error": result.error}
        (folder / "counters.json").write_text(json.dumps(data, indent=2) + "\n")

    def counters(c: Counters) -> None:
        write_counters(c)
        if on_counters is not None:
            on_counters(c)

    def finish(result: Result) -> None:
        try:
            if recorder is not None:
                recorder.finish()
                if recording is not None and recording.exists():
                    log(f"[{clock.stamp()}] recorded the source audio to {recording}")
            write_counters(result.counters, result)
        finally:
            if target is not None:
                target.close()
            with lock:
                log_file.close()
        if on_finish is not None:
            on_finish(result)

    keyboard: Keyboard = target if target is not None else NoKeyboard()
    engine = Engine(
        p,
        listener,
        keyboard,
        clock,
        focused=target.focused if target is not None else None,
        dry_run=dry_run,
        inhibitor=None if dry_run else backend.inhibitor(),
        on_log=log,
        on_counters=counters,
        on_meter=on_meter,
        on_state=on_state,
        on_finish=finish,
    )
    return Run(engine, folder, recording)
