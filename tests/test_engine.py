from __future__ import annotations

import math
from pathlib import Path

import pytest

from fakes import FakeClock, FakeInhibitor, FakeKeyboard, FakeListener, ForbiddenKeyboard
from sound_reactor import profile as profiles
from sound_reactor.engine import Engine, Result

EXAMPLE = Path(__file__).parent.parent / "examples" / "wow-fishing.toml"


def fishing(**stop) -> profiles.Profile:
    p = profiles.load(EXAMPLE)
    p.stop = profiles.StopRules(**stop)
    return p


def run(
    p: profiles.Profile,
    sounds=(),
    *,
    focus=lambda t: t >= 2.0,  # the user switches into the game at t = 2
    dry_run: bool = False,
    held=lambda t: None,
    inhibitor=None,
    setup=None,  # called with (engine, clock) before the run
    **listener_args,
):
    clock = inhibitor.clock if inhibitor is not None else FakeClock()
    listener = FakeListener(clock, list(sounds), **listener_args)
    keyboard = ForbiddenKeyboard() if dry_run else FakeKeyboard(clock, held)
    logs: list[str] = []
    meters = []
    engine = Engine(
        p,
        listener,
        keyboard,
        clock,
        focused=None if dry_run else (lambda: focus(clock.t)),
        dry_run=dry_run,
        inhibitor=inhibitor,
        on_log=logs.append,
        on_meter=meters.append,
    )
    if setup is not None:
        setup(engine, clock)
    result = engine.run()
    return result, keyboard, listener, logs, meters


def keys(keyboard: FakeKeyboard) -> list[tuple[float, str]]:
    return [(round(t, 2), k) for t, k in keyboard.pressed]


def test_fishing_cycle_starts_after_switch_and_delay_then_casts_loots_and_recasts():
    result, kb, listener, logs, _ = run(fishing(max_cycles=3), [(17.0, "bite", 0.9)])
    # switch in at 2 s, +5 s delay: cast; bite heard 17.3, Interact 0.25 s later, recast 2.5 s
    # after that; then two silent cycles of 30 s, and the limit stops before a third recast.
    assert keys(kb) == [(7.0, "9"), (17.55, "f"), (20.05, "9"), (50.05, "9")]
    assert result.counters.cycles == 3
    assert result.counters.triggers == {"bite": 1}
    assert result.counters.silences == 2
    assert "max_cycles" in result.reason and not result.error
    assert listener.started and listener.stopped
    assert any("BITE 10.0 s into the cycle" in line for line in logs)


def test_already_focused_target_needs_a_switch_into_it():
    focus = lambda t: t < 5.0 or t >= 8.0  # noqa: E731
    _, kb, *_ = run(fishing(max_cycles=1), focus=focus)
    assert kb.pressed[0][0] == pytest.approx(13.0, abs=0.11)


def test_ignores_sounds_after_keys_and_during_actions_once_per_cycle():
    sounds = [
        (7.5, "bite", 0.9),  # 0.5 s after the cast: the cast itself
        (15.0, "bite", 0.9),  # trigger
        (15.5, "bite", 0.95),  # reported while its actions run: once per cycle
        (18.5, "bite", 0.9),  # 0.45 s after the recast
        (25.0, "bite", 0.9),  # trigger
    ]
    result, kb, _, logs, _ = run(fishing(max_cycles=2), sounds)
    c = result.counters
    assert c.triggers == {"bite": 2} and c.ignored == 3 and c.silences == 0
    assert keys(kb) == [(7.0, "9"), (15.55, "f"), (18.05, "9"), (25.55, "f"), (28.05, "9")]
    assert sum("reported while actions were running" in line for line in logs) == 1
    assert sum("after the last key" in line for line in logs) == 2


def test_sound_without_rule_does_not_trigger():
    result, _, _, logs, _ = run(fishing(max_cycles=1), [(12.0, "door", 0.9)])
    assert result.counters.silences == 1 and result.counters.triggers == {"bite": 0}
    assert any("door" in line and "no rule" in line for line in logs)


def test_silent_cycles_in_a_row_stop_without_recasting():
    result, kb, *_ = run(fishing(max_silent_in_row=2))
    assert keys(kb) == [(7.0, "9"), (37.0, "9")]
    assert "max_silent_in_row" in result.reason


def test_a_trigger_resets_the_silent_count():
    result, *_ = run(fishing(max_silent_in_row=2), [(40.0, "bite", 0.9)])
    assert result.counters.triggers == {"bite": 1}
    assert result.counters.silences == 3  # one before the bite, two after it


def test_time_limit():
    result, kb, *_ = run(fishing(max_minutes=1.0))
    assert keys(kb) == [(7.0, "9"), (37.0, "9")]
    assert "max_minutes" in result.reason
    assert result.counters.minutes == pytest.approx(1.0, abs=0.01)


def test_focus_loss_stops_at_once():
    result, kb, listener, *_ = run(fishing(), [(40.0, "bite", 0.9)], focus=lambda t: 2 <= t < 30)
    assert keys(kb) == [(7.0, "9")]
    assert "lost focus" in result.reason
    assert listener.stopped


def test_focus_loss_during_start_delay_sends_nothing():
    result, kb, *_ = run(fishing(), focus=lambda t: 2 <= t < 4)
    assert kb.pressed == []
    assert "lost focus" in result.reason


def test_audio_disconnect_stops():
    result, kb, *_ = run(fishing(), [(25.0, "bite", 0.9)], disconnect_at=20.0)
    assert keys(kb) == [(7.0, "9")]
    assert "disconnected" in result.reason and result.error


def test_no_audio_at_start():
    result, kb, *_ = run(fishing(), connect_at=math.inf)
    assert kb.pressed == [] and "no audio" in result.reason and result.error


def test_held_key_stops_before_sending():
    held = lambda t: "key(s) w" if t > 10 else None  # noqa: E731
    result, kb, *_ = run(fishing(), [(15.0, "bite", 0.9)], held=held)
    assert keys(kb) == [(7.0, "9")]  # Interact was not sent
    assert "key(s) w held" in result.reason and result.error


def test_dry_run_needs_no_focus_and_sends_nothing():
    result, _, _, logs, _ = run(fishing(max_cycles=2), [(5.0, "bite", 0.9)], dry_run=True)
    assert result.counters.triggers == {"bite": 1}
    # cast, Interact, recast; the silent second cycle reaches max_cycles before recasting
    assert result.counters.keys == 3
    assert sum("(dry run: not sent)" in line for line in logs) == 3


def test_end_of_recording_stops():
    result, *_ = run(fishing(), dry_run=True, end_at=45.0)
    assert result.reason == "the audio source ended" and not result.error


def test_stop_request():
    p = fishing()
    clock = FakeClock()
    engine = Engine(p, FakeListener(clock), ForbiddenKeyboard(), clock, dry_run=True)
    engine.on_log = lambda line: "cycle 2" in line and engine.stop("Ctrl+C")
    engine.start()
    assert engine.join(5.0)
    assert isinstance(engine.result, Result)
    assert engine.result.reason == "Ctrl+C" and engine.result.counters.cycles == 1


def test_meter_about_once_a_second():
    *_, meters = run(fishing(max_cycles=1), dry_run=True)
    assert 28 <= len(meters) <= 31
    assert meters[0].peaks == {"bite": 0.5}


def test_live_run_needs_a_focus_check():
    clock = FakeClock()
    with pytest.raises(ValueError):
        Engine(fishing(), FakeListener(clock), FakeKeyboard(clock), clock)


def test_live_run_inhibits_the_screen_lock_from_the_switch_until_it_stops():
    inhibitor = FakeInhibitor(FakeClock())
    _, _, _, logs, _ = run(fishing(max_cycles=1), inhibitor=inhibitor)
    assert [(round(t, 2), call) for t, call in inhibitor.calls] == [
        (2.0, "inhibit"),
        (37.0, "release"),
    ]
    assert any("screen lock inhibited" in line and "fake cookie 7" in line for line in logs)
    assert any("inhibition released" in line for line in logs)


def test_inhibit_failure_is_logged_and_the_run_goes_on():
    inhibitor = FakeInhibitor(FakeClock(), fail=True)
    _, kb, _, logs, _ = run(fishing(max_cycles=1), inhibitor=inhibitor)
    assert [call for _, call in inhibitor.calls] == ["inhibit"]  # nothing to release
    assert any("WARNING: could not keep the screen from locking" in line for line in logs)
    assert keys(kb) == [(7.0, "9")]


def test_released_when_focus_is_lost():
    inhibitor = FakeInhibitor(FakeClock())
    run(fishing(), focus=lambda t: 2 <= t < 20, inhibitor=inhibitor)
    assert [(round(t, 1), call) for t, call in inhibitor.calls] == [
        (2.0, "inhibit"),
        (20.0, "release"),
    ]


def test_dry_run_does_not_inhibit():
    inhibitor = FakeInhibitor(FakeClock())
    run(fishing(max_cycles=1), dry_run=True, inhibitor=inhibitor)
    assert inhibitor.calls == []


# -- stopping safely in the middle of things ---------------------------------------------------


def test_trigger_reaching_max_cycles_runs_its_actions_then_stops():
    result, kb, *_ = run(fishing(max_cycles=1), [(15.0, "bite", 0.9)])
    assert keys(kb) == [(7.0, "9"), (15.55, "f"), (18.05, "9")]
    assert result.counters.cycles == 1 and "max_cycles" in result.reason and not result.error


def test_stop_in_the_middle_of_an_action_list():
    def setup(engine, clock):
        log = engine.on_log
        engine.on_log = lambda line: (log(line), "key f" in line and engine.stop("Stop button"))

    result, kb, *_ = run(fishing(), [(15.0, "bite", 0.9)], setup=setup)
    assert keys(kb) == [(7.0, "9"), (15.55, "f")]  # no recast
    assert result.reason == "Stop button" and not result.error


def test_focus_loss_in_the_middle_of_an_action_list():
    result, kb, *_ = run(fishing(), [(15.0, "bite", 0.9)], focus=lambda t: 2 <= t < 16.5)
    assert keys(kb) == [(7.0, "9"), (15.55, "f")]
    assert "lost focus" in result.reason


def test_focus_loss_between_the_held_check_and_the_press():
    state = {"focused": True}

    def held(t):
        if t > 10:
            state["focused"] = False  # e.g. Alt+Tab while the held check runs
        return None

    result, kb, *_ = run(
        fishing(), [(15.0, "bite", 0.9)], held=held, focus=lambda t: t >= 2 and state["focused"]
    )
    assert keys(kb) == [(7.0, "9")]
    assert "lost focus" in result.reason


def test_stop_between_the_held_check_and_the_press():
    box = {}

    def held(t):
        if t > 10:
            box["engine"].stop("Stop button")
        return None

    result, kb, *_ = run(
        fishing(), [(15.0, "bite", 0.9)], held=held, setup=lambda e, c: box.update(engine=e)
    )
    assert keys(kb) == [(7.0, "9")] and result.reason == "Stop button"


def test_held_input_before_the_first_cast():
    result, kb, *_ = run(fishing(), held=lambda t: "mouse button(s) 1")
    assert kb.pressed == []
    assert "mouse button(s) 1 held before key '9'" in result.reason and result.error


def test_held_input_before_the_recast():
    held = lambda t: "key(s) w" if t > 16 else None  # noqa: E731 (after f at 15.55)
    result, kb, *_ = run(fishing(), [(15.0, "bite", 0.9)], held=held)
    assert keys(kb) == [(7.0, "9"), (15.55, "f")]
    assert "key(s) w held before key '9'" in result.reason and result.error


def test_live_capture_ending_is_an_error():
    result, kb, *_ = run(fishing(), end_at=20.0, finite=False)
    assert keys(kb) == [(7.0, "9")]
    assert result.error and "stopped unexpectedly" in result.reason


def test_live_run_needs_the_minimum_start_delay():
    p = fishing()
    p.start_delay = 2.0
    clock = FakeClock()
    with pytest.raises(ValueError, match="start delay of at least 5 s"):
        Engine(p, FakeListener(clock), FakeKeyboard(clock), clock, focused=lambda: True)
    Engine(p, FakeListener(clock), ForbiddenKeyboard(), clock, dry_run=True)  # dry runs may
