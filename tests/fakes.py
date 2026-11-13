"""Fakes for the engine: a clock that only moves when someone waits, scheduled detections,
a keyboard that records, and synthetic audio for replays."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from sound_event_detector import Library, save_wav

from sound_reactor.engine import Heard, Meter


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def stamp(self) -> str:
        return f"{self.t:7.2f}"


class FakeListener:
    """Detections at fixed clock times; waiting for one moves the clock forward."""

    def __init__(
        self,
        clock: FakeClock,
        sounds: list[tuple[float, str, float]] = (),  # (sound start, name, similarity)
        *,
        latency: float = 0.3,
        connect_at: float = 0.0,
        disconnect_at: float = math.inf,
        end_at: float = math.inf,
        finite: bool = True,
    ) -> None:
        self.clock = clock
        self.finite = finite
        self.pending = sorted(
            (Heard(name, t + latency, t, sim, -30.0, f"{name}-1") for t, name, sim in sounds),
            key=lambda h: h.at,
        )
        self.connect_at = connect_at
        self.disconnect_at = disconnect_at
        self.end_at = end_at
        self.started = self.stopped = False
        self.returned: list[Heard] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def connected(self) -> bool:
        return self.connect_at <= self.clock.t < self.disconnect_at

    def ended(self) -> bool:
        return self.clock.t >= self.end_at

    def next(self, until: float) -> Heard | None:
        assert math.isfinite(until)
        if self.pending and self.pending[0].at <= until:
            h = self.pending.pop(0)
            self.clock.t = max(self.clock.t, h.at)
            self.returned.append(h)
            return h
        self.clock.t = max(self.clock.t, until)
        return None

    def meter(self) -> Meter:
        return Meter({"bite": 0.5}, -30.0)


class FakeKeyboard:
    def __init__(self, clock: FakeClock, held=lambda t: None) -> None:
        self.clock = clock
        self.held_at = held
        self.pressed: list[tuple[float, str]] = []

    def held(self) -> str | None:
        return self.held_at(self.clock.t)

    def press(self, key: str) -> None:
        self.pressed.append((self.clock.t, key))


class ForbiddenKeyboard:
    """A dry run must never touch the keyboard."""

    def held(self) -> str | None:
        raise AssertionError("dry run asked for held keys")

    def press(self, key: str) -> None:
        raise AssertionError(f"dry run sent {key!r}")


# -- synthetic audio ---------------------------------------------------------------------------

RATE = 22050


def chirp(rate: int = RATE, seconds: float = 0.25) -> np.ndarray:
    """A short rising chirp with a harmonic: distinctive enough to match only itself."""
    t = np.arange(int(rate * seconds)) / rate
    phase = 2 * np.pi * (1000 * t + 4000 * t * t)  # 1000 -> 3000 Hz
    env = np.hanning(len(t)) ** 0.5
    return (0.3 * env * (np.sin(phase) + 0.4 * np.sin(2.3 * phase))).astype(np.float32)


def session(times: list[float], length: float, rate: int = RATE, seed: int = 1) -> np.ndarray:
    """Noise at about -40 dBFS with the chirp starting at ``times`` (seconds)."""
    rng = np.random.default_rng(seed)
    audio = (0.01 * rng.standard_normal(int(length * rate))).astype(np.float32)
    clip = chirp(rate)
    for t in times:
        a = int(t * rate)
        audio[a : a + len(clip)] += clip
    return audio


def make_replay(folder: Path, times: list[float], length: float) -> tuple[Path, Library]:
    """A recording with chirps at ``times`` and a library (in ``folder``) that knows the chirp."""
    wav = folder / "session.wav"
    save_wav(wav, session(times, length), RATE)
    library = Library(folder / "library")
    library.add("chirp", chirp(), RATE)
    return wav, library


class FakeInhibitor:
    def __init__(self, clock: FakeClock, fail: bool = False) -> None:
        self.clock = clock
        self.fail = fail
        self.calls: list[tuple[float, str]] = []

    def inhibit(self, reason: str) -> str:
        self.calls.append((self.clock.t, "inhibit"))
        if self.fail:
            raise ConnectionError("no session bus")
        return "fake cookie 7"

    def release(self) -> None:
        self.calls.append((self.clock.t, "release"))


class FakeTarget:
    """Stands in for :class:`sound_reactor.x11.Target`: focus, held input, keys, keymap."""

    def __init__(self, clock: FakeClock | None = None, *, focus=lambda t: True, bad_keys=()):
        self.clock = clock or FakeClock()
        self.focus = focus
        self.bad_keys = set(bad_keys)
        self.checked: list[str] = []
        self.pressed: list[tuple[float, str]] = []
        self.closed = False

    def keycode(self, key: str) -> int:
        self.checked.append(key)
        if key in self.bad_keys:
            raise ValueError(f"key {key!r} needs a modifier on this layout")
        return 10

    def focused(self) -> bool:
        return self.focus(self.clock.t)

    def held(self) -> str | None:
        return None

    def press(self, key: str) -> None:
        self.pressed.append((self.clock.t, key))

    def close(self) -> None:
        self.closed = True
