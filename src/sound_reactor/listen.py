"""Where detections come from: live capture, or a recording replayed in audio time.

* :class:`LiveListener` wraps the detector library's :class:`SoundDetector` (capture thread,
  reconnects) and puts detections on the system clock.
* :class:`ReplayListener` feeds a WAV file to the library's :class:`Matcher` itself, as fast as
  the matcher runs, and its :attr:`ReplayListener.clock` is the audio position. Waits and
  timeouts in the profile then mean seconds *of the recording*, so a replay shows what a live
  run would have done at those moments, in a few seconds instead of in real time. (The
  library's ``file:`` source plays at real-time pace when used through ``SoundDetector``.)
* :class:`Recorder` wraps any source and writes what is read from it to a WAV file.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
import wave
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO

import numpy as np
from sound_event_detector import Detection, Matcher, SoundDetector, SoundEvent, Source
from sound_event_detector.audio import level_db, to_mono
from sound_event_detector.capture import DEFAULT_BLOCK, EndOfStream

from .engine import Clock, Heard, Meter

SILENT_DB = -120.0


class Recorder:
    """A source that also writes everything read from it to a 16-bit WAV file.

    The file must not exist yet (it is never overwritten). It stays open across reconnects
    (gaps in capture are simply missing from it) until :meth:`finish`.
    """

    def __init__(self, source: Source, path: str | Path) -> None:
        self.source = source
        self.path = Path(path)
        self.sample_rate = source.sample_rate
        self.channels = source.channels
        self._wav: wave.Wave_write | None = None
        self._file: BinaryIO | None = None
        self._lock = threading.Lock()
        self._done = False

    @property
    def name(self) -> str:
        return self.source.name

    def open(self) -> None:
        self.source.open()
        with self._lock:
            if self._wav is None and not self._done:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                file = self.path.open("xb")  # "x": never overwrite; open until finish()
                try:
                    wav = wave.open(file, "wb")  # noqa: SIM115 (open until finish())
                    wav.setnchannels(self.channels)
                    wav.setsampwidth(2)
                    wav.setframerate(self.sample_rate)
                except BaseException:
                    file.close()
                    raise
                self._file, self._wav = file, wav

    def read(self, frames: int) -> np.ndarray:
        block = self.source.read(frames)
        if len(block):
            pcm = (np.clip(block, -1.0, 1.0) * 32767.0).round().astype("<i2")
            with self._lock:
                if self._wav is not None:
                    self._wav.writeframes(pcm.tobytes())
        return block

    def close(self) -> None:
        self.source.close()

    def stale(self) -> bool:
        return self.source.stale()

    def finish(self) -> None:
        """Close the WAV file (safe to call more than once)."""
        with self._lock:
            self._done = True
            wav, self._wav = self._wav, None
            file, self._file = self._file, None
        try:
            if wav is not None:
                wav.close()
        finally:
            if file is not None:
                file.close()


def _heard(d: Detection, at: float) -> Heard:
    return Heard(d.name, at, at - d.latency, d.similarity, d.level_db, d.template)


class _ToRunLog(logging.Handler):
    """Passes the detector library's warnings and errors (capture interrupted, capture thread
    failed...) to the run log."""

    def __init__(self, write: Callable[[str], object]) -> None:
        super().__init__(logging.WARNING)
        self.write = write

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                e = record.exc_info[1]
                message += f": {type(e).__name__}: {e}"
            self.write(f"audio {record.levelname.lower()}: {message}")
        except Exception:
            self.handleError(record)


class LiveListener:
    """Detections from live capture, timed on ``clock`` (normally the system clock).

    ``on_log`` receives the detector library's warnings and errors as log lines.
    """

    finite = False  # live audio has no expected end: if capture stops, something failed

    def __init__(
        self,
        events: Sequence[SoundEvent],
        source: Source,
        clock: Clock,
        *,
        on_log: Callable[[str], object] | None = None,
    ) -> None:
        self.detector = SoundDetector(list(events), source=source)
        self.clock = clock
        self._queue: queue.Queue[Heard] = queue.Queue()
        self._started = False
        self._handler = (
            _ToRunLog(lambda message: on_log(f"[{clock.stamp()}] {message}"))
            if on_log is not None
            else None
        )
        self.detector.on_detect(self._on_detect)

    def _on_detect(self, d: Detection) -> None:
        age = max(0.0, time.time() - d.timestamp.timestamp()) if d.timestamp else 0.0
        self._queue.put(_heard(d, self.clock.now() - age))

    def start(self) -> None:
        if self._handler is not None:
            logging.getLogger("sound_event_detector").addHandler(self._handler)
        self.detector.start()
        self._started = True

    def stop(self) -> None:
        try:
            self.detector.stop()
        finally:
            if self._handler is not None:
                logging.getLogger("sound_event_detector").removeHandler(self._handler)

    def connected(self) -> bool:
        return self.detector.connected.is_set()

    def ended(self) -> bool:
        return self._started and not self.detector.running

    def next(self, until: float) -> Heard | None:
        timeout = until - self.clock.now()
        try:
            return self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None

    def meter(self) -> Meter:
        return Meter(self.detector.take_peaks(), self.detector.stats().level_db)


class AudioClock:
    """Seconds of audio a :class:`Matcher` has consumed."""

    def __init__(self, matcher: Matcher) -> None:
        self.matcher = matcher

    def now(self) -> float:
        return self.matcher.time

    def stamp(self) -> str:
        minutes, seconds = divmod(self.matcher.time, 60.0)
        return f"{int(minutes):02d}:{seconds:04.1f}"


class ReplayListener:
    """A finite source (a WAV file) analysed only as far as the engine's clock has to go."""

    finite = True

    def __init__(self, events: Sequence[SoundEvent], source: Source) -> None:
        self.matcher = Matcher(list(events))
        if source.sample_rate != self.matcher.sample_rate:
            raise ValueError(f"the source must deliver {self.matcher.sample_rate} Hz")
        self.source = source
        self.clock = AudioClock(self.matcher)
        self._pending: deque[Heard] = deque()
        self._open = False
        self._finished = False
        self._level = SILENT_DB

    def start(self) -> None:
        self.source.open()
        self._open = True

    def stop(self) -> None:
        if self._open:
            self._open = False
            self.source.close()

    def connected(self) -> bool:
        return self._open and not self.ended()

    def ended(self) -> bool:
        return self._finished and not self._pending

    def next(self, until: float) -> Heard | None:
        rate = self.matcher.sample_rate
        while not self._pending and self._open and not self._finished:
            remaining = until - self.matcher.time
            if remaining <= 0:
                break
            frames = DEFAULT_BLOCK if math.isinf(remaining) else math.ceil(remaining * rate)
            try:
                block = self.source.read(min(DEFAULT_BLOCK, frames))
            except EndOfStream:
                found = self.matcher.flush()
                self._finished = True
            else:
                mono = to_mono(block)
                if len(mono):
                    self._level = max(self._level, level_db(mono))
                found = self.matcher.feed(mono)
            self._pending.extend(_heard(d, d.detected_at) for d in found)
        return self._pending.popleft() if self._pending else None

    def meter(self) -> Meter:
        level, self._level = self._level, SILENT_DB
        return Meter(self.matcher.take_peaks(), level)
