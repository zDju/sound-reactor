"""The reaction cycle: hear a sound, run its rule's actions, repeat, and stop safely.

The engine knows nothing about Qt, audio devices or X11. The sound side (a :class:`Listener`),
the :class:`Keyboard`, the focus check and the :class:`Clock` are passed in, so the same cycle
runs against live audio, a recording replayed in audio time, or test fakes. It runs in its own
thread (:meth:`Engine.start`) or the caller's (:meth:`Engine.run`) and reports log lines,
counters and meter values through callbacks, which are called from that thread.

Cycle (generalised from ``spikes/fish_cycle.py``):

* A cycle starts when an action list finishes (``on_start``, an ``on_sound`` rule or
  ``on_silence``).
* Sounds that start within ``ignore_after_action`` seconds of the last key are ignored, and so
  are sounds reported while actions were running.
* The first matching sound in a cycle runs its rule's actions; nothing else triggers until they
  finish.
* No trigger within ``on_silence.seconds`` of the cycle start: ``on_silence`` actions, new cycle.
* Stop on: target losing focus, the audio disconnecting or ending, the profile's limits, a key
  or mouse button held before a key is sent, or :meth:`Engine.stop`.

A live run only *arms* at first: it begins when the target window gains focus after not having
it (Alt+Tab into it), followed by ``start_delay`` seconds. From that switch until it stops it
holds an :class:`Inhibitor`, so that an idle screen lock does not take focus (sent keys do not
count as user activity). A dry run needs no focus, starts at once, never calls the keyboard and
inhibits nothing.
"""

from __future__ import annotations

import math
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from .profile import Action, Key, Profile

AUDIO_TIMEOUT = 10.0  # s to wait for the source to deliver audio
POLL = 0.1  # s between stop/focus checks while waiting
MIN_LIVE_START_DELAY = 5.0  # s between switching into the target and the first key (live)


@dataclass(frozen=True)
class Heard:
    """A detected sound, with times on the engine's clock."""

    sound: str
    at: float  # when it was reported
    started: float  # when the sound began
    similarity: float
    level_db: float
    template: str = ""


@dataclass(frozen=True)
class Meter:
    """Each sound's best similarity over about the last second (-1: nothing was scored, e.g.
    too quiet) and the source level."""

    peaks: dict[str, float]
    level_db: float


@dataclass
class Counters:
    cycles: int = 0  # cycles ended by a trigger or by silence
    keys: int = 0  # keys sent (or logged, in a dry run)
    triggers: dict[str, int] = field(default_factory=dict)
    silences: int = 0
    silent_in_row: int = 0
    ignored: int = 0  # sounds heard but ignored (too soon after a key, during actions)
    minutes: float = 0.0  # since the first action


@dataclass(frozen=True)
class Result:
    reason: str
    counters: Counters
    error: bool = False


class Clock(Protocol):
    def now(self) -> float:
        """Seconds on a monotonic clock."""
        ...

    def stamp(self) -> str:
        """The current time as shown in log lines."""
        ...


class Listener(Protocol):
    """The sound side: detections and the state of the audio source."""

    finite: bool  # True: the source is a recording, whose end is expected

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def connected(self) -> bool:
        """Audio is being received."""
        ...

    def ended(self) -> bool:
        """The source is finished for good (end of a recording, or capture failed)."""
        ...

    def next(self, until: float) -> Heard | None:
        """The next detection, waiting until clock time ``until`` at most (None: none came)."""
        ...

    def meter(self) -> Meter:
        """Peaks and level since the previous call."""
        ...


class Keyboard(Protocol):
    def held(self) -> str | None:
        """What keys or mouse buttons are held right now (None: nothing)."""
        ...

    def press(self, key: str) -> None:
        """Press and release ``key`` in the target window."""
        ...


class Inhibitor(Protocol):
    """Keeps the screen from locking (an idle lock would take focus and stop a live run)."""

    def inhibit(self, reason: str) -> str:
        """Hold the inhibition; returns a description for the log. Raises if it cannot."""
        ...

    def release(self) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def stamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S")


class _Stop(Exception):
    def __init__(self, reason: str, *, error: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error


def _nothing(*_: object) -> None:
    pass


class Engine:
    def __init__(
        self,
        profile: Profile,
        listener: Listener,
        keyboard: Keyboard,
        clock: Clock,
        *,
        focused: Callable[[], bool] | None = None,
        dry_run: bool = False,
        inhibitor: Inhibitor | None = None,
        on_log: Callable[[str], object] = _nothing,
        on_counters: Callable[[Counters], object] = _nothing,
        on_meter: Callable[[Meter], object] | None = None,
        on_finish: Callable[[Result], object] = _nothing,
        poll: float = POLL,
    ) -> None:
        if not dry_run and focused is None:
            raise ValueError("a run that sends keys needs a focus check")
        if not dry_run and profile.start_delay < MIN_LIVE_START_DELAY:
            raise ValueError(
                f"a live run needs a start delay of at least {MIN_LIVE_START_DELAY:g} s"
            )
        self.profile = profile
        self.listener = listener
        self.keyboard = keyboard
        self.clock = clock
        self.focused = focused or (lambda: True)
        self.dry_run = dry_run
        self.inhibitor = None if dry_run else inhibitor
        self._inhibited = False
        self.on_log = on_log
        self.on_counters = on_counters
        self.on_meter = on_meter
        self.on_finish = on_finish
        self.poll = poll
        self.rules = {r.sound: r for r in profile.on_sound}
        self.counters = Counters(triggers=dict.fromkeys(self.rules, 0))
        self.result: Result | None = None
        self._stop = threading.Event()
        self._stop_reason = "stopped"
        self._thread: threading.Thread | None = None
        self._t0: float | None = None  # clock time of the first action
        self._last_key = -math.inf
        self._next_meter = math.inf

    # -- control (any thread) ------------------------------------------------------------------

    def start(self) -> None:
        """Run in a background thread."""
        if self._thread is not None:
            raise RuntimeError("the engine has already been started")
        self._thread = threading.Thread(target=self.run, name="sound-reactor", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "stopped") -> None:
        """Ask the run to stop; it does within :data:`POLL` seconds or after the key being sent."""
        if not self._stop.is_set():
            self._stop_reason = reason
            self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the background run to end; True if it has."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the run -------------------------------------------------------------------------------

    def run(self) -> Result:
        reason, error = "stopped", False
        try:
            self._describe()
            self.listener.start()
            self._next_meter = self.clock.now() + 1.0
            self._await_audio()
            self._arm()
            self._t0 = self.clock.now()
            self._log("start")
            self._actions(self.profile.on_start)
            while True:
                self._cycle()
        except _Stop as s:
            reason, error = s.reason, s.error
        except Exception as e:
            reason, error = f"error: {type(e).__name__}: {e}", True
            for line in traceback.format_exc().rstrip().splitlines():
                self._log(line)
        finally:
            self._uninhibit()
            try:
                self.listener.stop()
            except Exception as e:
                self._log(f"error while stopping the audio: {type(e).__name__}: {e}")
            self._update_minutes()
            c = self.counters
            self.result = Result(reason, replace(c, triggers=dict(c.triggers)), error)
            self._log(f"stopped: {reason}")
            triggers = ", ".join(f"{k} {v}" for k, v in c.triggers.items()) or "none"
            self._log(
                f"done: {c.cycles} cycles, {c.keys} keys{' (dry run)' if self.dry_run else ''}, "
                f"triggers: {triggers}; silent cycles {c.silences}; ignored sounds {c.ignored}"
            )
            self.on_finish(self.result)
        return self.result

    def _describe(self) -> None:
        p = self.profile
        rules = "; ".join(f"{r.sound} -> {len(r.actions)} steps" for r in p.on_sound) or "none"
        silence = f"{p.on_silence.seconds:g} s" if p.on_silence else "off"
        limits = ", ".join(
            f"{k} {v:g}" for k, v in vars(p.stop).items() if v is not None
        ) or "none"  # fmt: skip
        self._log(f"source {p.source}; target {p.target or '-'}; rules: {rules}")
        self._log(
            f"silence timeout {silence}; ignore {p.ignore_after_action:g} s after a key; "
            f"limits: {limits}"
        )

    def _await_audio(self) -> None:
        deadline = self.clock.now() + AUDIO_TIMEOUT
        while not self.listener.connected():
            if self._stop.is_set():
                raise _Stop(self._stop_reason)
            if self.listener.ended():
                raise _Stop("the audio source ended before it started", error=True)
            now = self.clock.now()
            if now >= deadline:
                raise _Stop(
                    f"no audio from {self.profile.source} in {AUDIO_TIMEOUT:g} s", error=True
                )
            self.listener.next(min(deadline, now + self.poll))
        self._log("audio connected")

    def _arm(self) -> None:
        if self.dry_run:
            self._log("dry run: keys are logged, not sent; no focus needed")
            return
        target = self.profile.target
        self._log(f"armed: switch to the window {target!r} to start")
        seen_other = False
        while True:
            self._check(focus=False)
            if not self.focused():
                seen_other = True
            elif seen_other:
                break
            self._pause(self.poll, "before the start", focus=False)
        self._log(f"{target!r} focused; starting in {self.profile.start_delay:g} s")
        self._inhibit()
        self._pause(self.profile.start_delay, "before the start")

    def _inhibit(self) -> None:
        if self.inhibitor is None:
            return
        try:
            how = self.inhibitor.inhibit(f"reacting to sounds in {self.profile.target!r}")
        except Exception as e:
            self._log(
                f"WARNING: could not keep the screen from locking ({type(e).__name__}: {e}); "
                "an idle screen lock will take focus and stop the run"
            )
            return
        self._inhibited = True
        self._log(f"screen lock inhibited while running ({how})")

    def _uninhibit(self) -> None:
        if not self._inhibited or self.inhibitor is None:
            return
        self._inhibited = False
        try:
            self.inhibitor.release()
            self._log("screen lock inhibition released")
        except Exception as e:
            self._log(f"could not release the screen lock inhibition: {type(e).__name__}: {e}")

    def _cycle(self) -> None:
        p, c = self.profile, self.counters
        start = self.clock.now()
        silence = p.on_silence
        deadline = start + silence.seconds if silence is not None else math.inf
        wait = f": waiting up to {silence.seconds:g} s" if silence is not None else ""
        self._log(f"cycle {c.cycles + 1}{wait}")
        while (h := self._wait(deadline)) is not None:
            since_key = h.started - self._last_key
            if h.at < start:
                self._ignore(h, "reported while actions were running")
            elif since_key < p.ignore_after_action:
                self._ignore(h, f"started {since_key:.2f} s after the last key")
            elif h.sound not in self.rules:
                self._log(f"heard {h.sound} ({h.similarity:.2f}); no rule for it")
            else:
                c.cycles += 1
                c.silent_in_row = 0
                c.triggers[h.sound] = c.triggers.get(h.sound, 0) + 1
                self._log(
                    f"{h.sound.upper()} {h.started - start:.1f} s into the cycle: similarity "
                    f"{h.similarity:.2f}, level {h.level_db:.1f} dBFS ({h.template})"
                )
                self._changed()
                self._actions(self.rules[h.sound].actions)
                self._check_limits()
                return
        assert silence is not None
        c.cycles += 1
        c.silences += 1
        c.silent_in_row += 1
        self._log(f"nothing in {silence.seconds:g} s")
        self._changed()
        self._check_limits()  # before the actions: they only start another cycle
        self._actions(silence.actions)

    def _check_limits(self) -> None:
        s, c = self.profile.stop, self.counters
        if s.max_cycles is not None and c.cycles >= s.max_cycles:
            raise _Stop(f"{c.cycles} cycles done (max_cycles)")
        if s.max_silent_in_row is not None and c.silent_in_row >= s.max_silent_in_row:
            raise _Stop(f"{c.silent_in_row} cycles in a row without a trigger (max_silent_in_row)")

    # -- actions -------------------------------------------------------------------------------

    def _actions(self, actions: list[Action]) -> None:
        for a in actions:
            if isinstance(a, Key):
                self._press(a.key)
            else:
                self._pause(a.seconds, "reported while actions were running")

    def _press(self, key: str) -> None:
        self._check()
        if self.dry_run:
            self._log(f"  key {key} (dry run: not sent)")
        else:
            held = self.keyboard.held()
            if held:
                raise _Stop(f"{held} held before key {key!r}; not sent", error=True)
            self._check()
            self.keyboard.press(key)
            self._log(f"  key {key}")
        self._last_key = self.clock.now()
        self.counters.keys += 1
        self._changed()

    def _pause(self, seconds: float, why: str, *, focus: bool = True) -> None:
        until = self.clock.now() + seconds
        while (h := self._wait(until, focus=focus)) is not None:
            self._ignore(h, why)

    # -- waiting and checks --------------------------------------------------------------------

    def _wait(self, until: float, *, focus: bool = True) -> Heard | None:
        """The next detection before clock time ``until``, checking for stop reasons meanwhile."""
        while True:
            self._check(focus=focus)
            now = self.clock.now()
            if now >= until:
                return None
            heard = self.listener.next(min(until, now + self.poll))
            self._tick()
            if heard is not None:
                return heard

    def _check(self, *, focus: bool = True) -> None:
        if self._stop.is_set():
            raise _Stop(self._stop_reason)
        if focus and not self.dry_run and not self.focused():
            raise _Stop("the target window lost focus")
        if self.listener.ended():
            if self.listener.finite:
                raise _Stop("the audio source ended")
            raise _Stop("audio capture stopped unexpectedly (see the log)", error=True)
        if not self.listener.connected():
            raise _Stop("the audio source disconnected", error=True)
        limit = self.profile.stop.max_minutes
        if (
            limit is not None
            and self._t0 is not None
            and self.clock.now() - self._t0 >= limit * 60.0
        ):
            raise _Stop(f"time limit of {limit:g} min reached (max_minutes)")

    def _tick(self) -> None:
        now = self.clock.now()
        if self.on_meter is None or now < self._next_meter:
            return
        self._next_meter = now + 1.0
        self.on_meter(self.listener.meter())

    # -- reporting -----------------------------------------------------------------------------

    def _ignore(self, h: Heard, why: str) -> None:
        self.counters.ignored += 1
        self._log(f"  ignored {h.sound} ({h.similarity:.2f}): {why}")
        self._changed()

    def _update_minutes(self) -> None:
        if self._t0 is not None:
            self.counters.minutes = (self.clock.now() - self._t0) / 60.0

    def _changed(self) -> None:
        self._update_minutes()
        c = self.counters
        self.on_counters(replace(c, triggers=dict(c.triggers)))

    def _log(self, message: str) -> None:
        self.on_log(f"[{self.clock.stamp()}] {message}")
