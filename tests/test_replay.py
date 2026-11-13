"""Replays of recordings: real detection on synthetic audio, run folders, recording, and the CLI."""

from __future__ import annotations

import json
import logging
import signal
import time
import wave
from pathlib import Path

import pytest
from sound_event_detector import FileSource, Library, save_wav

from fakes import (
    RATE,
    FakeClock,
    FakeInhibitor,
    FakeListener,
    FakeTarget,
    chirp,
    make_replay,
    session,
)
from sound_reactor import cli, x11
from sound_reactor import profile as profiles
from sound_reactor.cli import main
from sound_reactor.engine import Counters, Result, SystemClock
from sound_reactor.listen import LiveListener, Recorder, ReplayListener
from sound_reactor.profile import Key, Profile, SilenceRule, SoundRule, StopRules, Wait
from sound_reactor.runner import NoKeyboard, RunError, prepare, problems
from sound_reactor.screensaver import ScreenSaverInhibitor

CHIRPS = [5.0, 12.0, 14.0, 40.0]  # the one at 14.0 is heard while the 12.0 one is answered
LENGTH = 50.0


def chirp_profile(wav: Path) -> Profile:
    return Profile(
        source=f"file:{wav}",
        sounds={"chirp": {"cooldown": 0.5}},
        on_start=[Key("9")],
        on_sound=[SoundRule("chirp", [Wait(0.25), Key("f"), Wait(2.5), Key("9")])],
        on_silence=SilenceRule(10.0, [Key("9")]),
    )


def spy(run) -> list:
    """Record what the run's listener reports to the engine."""
    heard = []
    listener = run.engine.listener
    original = listener.next

    def next_(until):
        h = original(until)
        if h is not None:
            heard.append(h)
        return h

    listener.next = next_
    return heard


def test_replay_runs_in_audio_time_and_records(tmp_path):
    wav, library = make_replay(tmp_path, CHIRPS, LENGTH)
    lines: list[str] = []
    run = prepare(
        chirp_profile(wav),
        name="chirps",
        dry_run=True,
        record=True,
        runs_root=tmp_path / "runs",
        library=library,
        on_log=lines.append,
    )
    heard = spy(run)
    run.start()
    assert run.engine.join(60.0)
    result = run.engine.result
    assert result is not None and result.reason == "the audio source ended" and not result.error
    c = result.counters
    assert c.triggers == {"chirp": 3} and c.ignored == 1 and c.silences == 2
    assert [round(h.started, 1) for h in heard] == CHIRPS
    assert all(h.similarity > 0.95 for h in heard)
    # times in the log are positions in the recording
    assert any(line.startswith("[00:25.0") and "nothing in 10 s" in line for line in lines)

    folder = run.folder
    assert folder.parent == tmp_path / "runs" and folder.name.endswith("-chirps")
    assert "CHIRP" in (folder / "log.txt").read_text()
    counters = json.loads((folder / "counters.json").read_text())
    assert counters["triggers"] == {"chirp": 3}
    assert counters["stop_reason"] == "the audio source ended"
    assert profiles.load(folder / "profile.toml") == chirp_profile(wav)
    with wave.open(str(folder / "audio.wav")) as w:
        assert (w.getframerate(), w.getnchannels()) == (RATE, 1)
        assert w.getnframes() == round(LENGTH * RATE)


def test_problems():
    p = chirp_profile(Path("/nonexistent.wav"))
    p.on_start.append(Key("notakey"))
    found = " | ".join(problems(p, dry_run=False))
    assert "run it as a dry run" in found
    assert "recording not found" in found
    assert "no target window" in found
    assert "unknown key 'notakey'" in found
    assert problems(Profile(), dry_run=True) == [
        "the profile has no source",
        "the profile has no sounds",
    ]


def test_unknown_sound(tmp_path):
    wav, library = make_replay(tmp_path, [], 1.0)
    p = chirp_profile(wav)
    p.on_sound[0].sound = "nothing"
    p.sounds = {}
    with pytest.raises(RunError, match="no event 'nothing'"):
        prepare(p, dry_run=True, runs_root=tmp_path / "runs", library=library)
    assert not (tmp_path / "runs").exists()


def test_cli_dry_run_replay(tmp_path, capsys):
    wav, _ = make_replay(tmp_path, CHIRPS, LENGTH)  # its library is the test's default library
    path = profiles.save(chirp_profile(tmp_path / "unused.wav"), profiles.resolve("chirps"))
    code = main(
        ["run", "chirps", "--dry-run", "--source", f"file:{wav}", "--meter",
         "--runs-dir", str(tmp_path / "runs")]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert f"profile {path}" in out
    assert out.count("CHIRP") == 3 and out.count("(dry run: not sent)") == 9
    assert "meter: chirp" in out and "done: 5 cycles" in out


def test_cli_refuses_a_replay_that_would_send_keys(tmp_path, capsys):
    wav, _ = make_replay(tmp_path, [], 1.0)
    path = profiles.save(chirp_profile(wav), tmp_path / "p.toml")
    assert main(["run", str(path), "--runs-dir", str(tmp_path / "runs")]) == 1
    assert "run it as a dry run" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_cli_bad_profile(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[stop]\nmax_cycles = 0\n")
    assert main(["run", str(bad), "--dry-run"]) == 2
    assert "max_cycles" in capsys.readouterr().err


def test_cli_lists_profiles(capsys):
    profiles.save(Profile(), profiles.resolve("one"))
    assert main(["profiles"]) == 0
    assert capsys.readouterr().out.split() == ["one"]


def test_two_detections_pending_at_the_end_of_a_recording(tmp_path):
    wav = tmp_path / "end.wav"
    save_wav(wav, session([5.0], 5.25), RATE)  # the chirp ends with the file
    library = Library(tmp_path / "library")
    library.add("chirp", chirp(), RATE)
    library.add("chirp2", chirp(), RATE)  # the same sound under a second name
    events = [library.load("chirp"), library.load("chirp2")]
    listener = ReplayListener(events, FileSource(wav, sample_rate=RATE, realtime=False))
    listener.start()
    first = listener.next(10.0)
    assert first is not None
    assert listener.clock.now() == pytest.approx(5.25, abs=0.001)  # found at the end of the file
    assert listener.connected() and not listener.ended()  # the second one is still to come
    second = listener.next(10.0)
    assert second is not None and {first.sound, second.sound} == {"chirp", "chirp2"}
    assert listener.ended() and not listener.connected()
    listener.stop()

    p = Profile(
        source=f"file:{wav}",
        sounds={"chirp": {}, "chirp2": {}},
        on_sound=[SoundRule("chirp", [Key("f")]), SoundRule("chirp2", [Key("g")])],
    )
    result = prepare(p, dry_run=True, runs_root=tmp_path / "runs", library=library).engine.run()
    assert result.reason == "the audio source ended" and not result.error


def test_recordings_never_overwrite_a_file(tmp_path):
    wav, library = make_replay(tmp_path, [5.0], 10.0)
    recording = wav.read_bytes()
    existing = tmp_path / "old.wav"
    existing.write_bytes(b"keep me")
    for path in (wav, existing):  # the replayed file itself, and any other file
        with pytest.raises(RunError, match="already exists"):
            prepare(
                chirp_profile(wav),
                dry_run=True,
                record=path,
                runs_root=tmp_path / "runs",
                library=library,
            )
    assert wav.read_bytes() == recording and existing.read_bytes() == b"keep me"
    assert not (tmp_path / "runs").exists()
    recorder = Recorder(FileSource(wav, sample_rate=RATE, realtime=False), existing)
    with pytest.raises(FileExistsError):  # also if the file appears after the check
        recorder.open()
    assert existing.read_bytes() == b"keep me"


def live_profile() -> Profile:
    return Profile(
        source="app:Test Game",  # never opened in these tests
        target="Game",
        sounds={"chirp": {}},
        on_start=[Key("9")],
        on_sound=[SoundRule("chirp", [Wait(0.25), Key("f"), Wait(2.5), Key("9")])],
        stop=StopRules(max_cycles=1),
    )


def use_target(monkeypatch, target) -> None:
    monkeypatch.setattr(x11.Target, "find", staticmethod(lambda title, display_name=None: target))


def test_live_prepare_wiring(tmp_path, monkeypatch):
    _, library = make_replay(tmp_path, [], 1.0)
    clock = FakeClock()
    target = FakeTarget(clock, focus=lambda t: t >= 2.0)
    use_target(monkeypatch, target)
    run = prepare(live_profile(), runs_root=tmp_path / "runs", library=library)
    engine = run.engine
    assert engine.keyboard is target and engine.focused == target.focused
    assert isinstance(engine.inhibitor, ScreenSaverInhibitor) and not engine.dry_run
    assert target.checked == ["9", "f"]  # every key checked against the live keymap
    # run it on fakes: no audio device, no session bus
    engine.listener = FakeListener(clock, [(15.0, "chirp", 0.9)])
    engine.clock = clock
    engine.inhibitor = FakeInhibitor(clock)
    result = engine.run()
    assert [(round(t, 2), k) for t, k in target.pressed] == [(7.0, "9"), (15.55, "f"), (18.05, "9")]
    assert "max_cycles" in result.reason and target.closed
    assert "screen lock inhibited" in (run.folder / "log.txt").read_text()
    assert json.loads((run.folder / "counters.json").read_text())["keys"] == 3


def test_dry_prepare_never_looks_for_the_window(tmp_path, monkeypatch):
    _, library = make_replay(tmp_path, [], 1.0)
    monkeypatch.setattr(x11.Target, "find", None)  # calling it would fail the test
    run = prepare(live_profile(), dry_run=True, runs_root=tmp_path / "runs", library=library)
    assert isinstance(run.engine.keyboard, NoKeyboard)
    assert run.engine.inhibitor is None and run.engine.dry_run
    run.engine.on_finish(Result("not run", Counters()))  # closes the run log


def test_live_prepare_refuses_keys_that_need_a_modifier(tmp_path, monkeypatch):
    _, library = make_replay(tmp_path, [], 1.0)
    target = FakeTarget(bad_keys={"exclam"})
    use_target(monkeypatch, target)
    p = live_profile()
    p.on_start = [Key("exclam")]
    with pytest.raises(RunError, match="needs a modifier"):
        prepare(p, runs_root=tmp_path / "runs", library=library)
    assert target.closed and not (tmp_path / "runs").exists()


def test_live_prepare_closes_the_window_when_a_later_step_fails(tmp_path, monkeypatch):
    _, library = make_replay(tmp_path, [], 1.0)
    target = FakeTarget()
    use_target(monkeypatch, target)
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    with pytest.raises(OSError):
        prepare(live_profile(), runs_root=blocker, library=library)
    assert target.closed


def test_live_run_needs_the_minimum_start_delay(tmp_path, monkeypatch):
    _, library = make_replay(tmp_path, [], 1.0)
    monkeypatch.setattr(x11.Target, "find", None)  # not reached
    p = live_profile()
    p.start_delay = 1.0
    with pytest.raises(RunError, match="at least 5 s for a live run"):
        prepare(p, runs_root=tmp_path / "runs", library=library)
    p.source = "file:/nonexistent.wav"
    assert not any("start_delay" in problem for problem in problems(p, dry_run=True))


class BrokenSource:
    """A source whose capture thread fails (a bug, not a missing device)."""

    name = "broken"
    sample_rate = RATE
    channels = 1

    def open(self) -> None:
        pass

    def read(self, frames: int):
        raise ValueError("boom")

    def close(self) -> None:
        pass

    def stale(self) -> bool:
        return False


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_failed_live_capture_is_logged(tmp_path):
    _, library = make_replay(tmp_path, [], 1.0)
    lines: list[str] = []
    listener = LiveListener(
        [library.load("chirp")], BrokenSource(), SystemClock(), on_log=lines.append
    )
    assert not listener.finite  # its end is a failure, not the end of a recording
    listener.start()
    deadline = time.monotonic() + 5.0
    while not listener.ended():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    listener.stop()
    assert any("audio error: capture thread failed: ValueError: boom" in line for line in lines)
    assert not logging.getLogger("sound_event_detector").handlers  # handler removed again


def test_cli_reports_prepare_failures_without_a_traceback(tmp_path, capsys):
    wav, _ = make_replay(tmp_path, [], 1.0)
    path = profiles.save(chirp_profile(wav), tmp_path / "p.toml")
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    assert main(["run", str(path), "--dry-run", "--runs-dir", str(blocker)]) == 1
    assert "cannot prepare the run" in capsys.readouterr().err


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_cli_stops_cleanly_on_sigterm_and_sighup(tmp_path, capsys, monkeypatch, sig):
    wav, _ = make_replay(tmp_path, CHIRPS, LENGTH)
    path = profiles.save(chirp_profile(wav), tmp_path / "p.toml")
    real_prepare = cli.prepare

    def prepare_then_signal(*args, **kwargs):
        run = real_prepare(*args, **kwargs)
        start = run.engine.start

        def signal_then_start():
            signal.raise_signal(sig)  # as if the terminal closed, or kill was used
            start()

        run.engine.start = signal_then_start
        return run

    monkeypatch.setattr(cli, "prepare", prepare_then_signal)
    unhandled = []

    def sentinel(*_):  # catches the signal if the CLI did not, instead of ending pytest
        unhandled.append(sig)

    previous = signal.signal(sig, sentinel)
    try:
        code = main(["run", str(path), "--dry-run", "--runs-dir", str(tmp_path / "runs")])
        assert signal.getsignal(sig) is sentinel  # the CLI put the old handler back
    finally:
        signal.signal(sig, previous)
    assert unhandled == [] and code == 0
    assert "stopped: SIG" in capsys.readouterr().out
    (counters,) = (tmp_path / "runs").glob("*/counters.json")
    assert json.loads(counters.read_text())["stop_reason"].startswith("SIG")


# -- a real recording (local only, never in the repository) ------------------------------------

PROJECTS = Path(__file__).resolve().parents[2]
RECORDING = PROJECTS / "sound-event-detector" / "recordings" / "wow-session-4.wav"
USER_LIBRARY = Path.home() / ".local" / "share" / "sound-event-detector"  # read, never written
BITES = [65.6, 86.7, 198.8, 227.3, 237.6]  # where the 5 real bites start


@pytest.mark.local
def test_wow_session_4(tmp_path):
    if not RECORDING.is_file() or "bite" not in Library(USER_LIBRARY).names():
        pytest.skip("needs the local recording and the user's 'bite' event")
    p = profiles.load(Path(__file__).parent.parent / "examples" / "wow-fishing.toml")
    p.source = f"file:{RECORDING}"
    run = prepare(p, dry_run=True, runs_root=tmp_path, library=Library(USER_LIBRARY))
    heard = spy(run)
    result = run.engine.run()
    assert result.counters.triggers == {"bite": 5} and result.counters.ignored == 0
    assert [h.started for h in heard] == pytest.approx(BITES, abs=0.3)
