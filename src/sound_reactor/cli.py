"""The ``sound-reactor`` command."""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
from pathlib import Path

from . import paths
from . import profile as profiles
from .doctor import distribution
from .engine import Meter
from .runner import RunError, prepare, runs_dir


def _print(line: str) -> None:
    # The terminal may be gone (SIGHUP); the run must still stop cleanly and write its folder.
    with contextlib.suppress(OSError, ValueError):
        print(line, flush=True)


# SIGHUP exists only on Unix; Windows has SIGBREAK (Ctrl+Break). Closing a Windows console is
# handled apart (win32.on_console_close): Windows ends the process as soon as its handler returns.
STOP_SIGNALS = {signal.SIGTERM: "SIGTERM"}
if hasattr(signal, "SIGHUP"):
    STOP_SIGNALS[signal.SIGHUP] = "SIGHUP (terminal closed)"
if hasattr(signal, "SIGBREAK"):
    STOP_SIGNALS[signal.SIGBREAK] = "Ctrl+Break"


def cmd_run(args: argparse.Namespace) -> int:
    path = profiles.resolve(args.profile)
    try:
        p = profiles.load(path)
    except profiles.ProfileError as e:
        print(f"sound-reactor: {e}", file=sys.stderr)
        return 2
    if args.source:
        p.source = args.source

    def meter(m: Meter) -> None:
        peaks = ", ".join(
            f"{name} {score:.2f}" if score > -1 else f"{name} -" for name, score in m.peaks.items()
        )
        _print(f"  meter: {peaks}; level {m.level_db:.1f} dBFS")

    try:
        run = prepare(
            p,
            name=path.stem,
            dry_run=args.dry_run,
            record=args.record or False,
            runs_root=args.runs_dir,
            on_log=_print,
            on_meter=meter if args.meter else None,
        )
    except RunError as e:
        print(f"sound-reactor: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # e.g. the runs folder is not writable
        print(f"sound-reactor: cannot prepare the run: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    _print(f"profile {path}; run folder {run.folder}")
    # Stop like Ctrl+C on SIGTERM/SIGHUP/SIGBREAK: the key being sent is released, and the log,
    # counters and recording are finished, instead of the process dying mid-key.
    from .win32 import on_console_close  # does nothing outside Windows

    previous = {
        sig: signal.signal(sig, lambda signum, _frame: run.stop(STOP_SIGNALS[signum]))
        for sig in STOP_SIGNALS
    }
    try:
        with on_console_close(run.stop, run.engine.join):
            run.start()
            while True:
                try:
                    if run.engine.join(0.2):
                        break
                except KeyboardInterrupt:
                    run.stop("Ctrl+C")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    result = run.engine.result
    return 1 if result is None or result.error else 0


def cmd_profiles(args: argparse.Namespace) -> int:
    folder = profiles.profiles_dir()
    names = profiles.list_profiles()
    if not names:
        print(f"no profiles in {folder}")
    for name in names:
        print(name)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import main_text

    text, code = main_text(args.profile)
    print(text, end="", flush=True)
    return code


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main as gui_main

    return gui_main(args.profile)


class _Version(argparse.Action):
    """``--version``: the version and where it was installed from, on one line (argparse's own
    version action wraps it)."""

    def __init__(self, option_strings: list[str], dest: str = argparse.SUPPRESS, **_: object):
        super().__init__(
            option_strings, dest, nargs=0, default=argparse.SUPPRESS,
            help="show the version and where it was installed from, and exit",
        )  # fmt: skip

    def __call__(self, parser: argparse.ArgumentParser, *_: object) -> None:
        print(f"sound-reactor {distribution('sound-reactor')}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sound-reactor",
        description="React to sound events with key presses in one window (an X11 window on "
        "Linux; on Windows, the foreground window when it is the target).",
    )
    parser.add_argument("--version", action=_Version)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="run a profile",
        description=(
            "Run a profile. A live run arms first: it starts when you switch into the target "
            "window, after the profile's start delay, and stops when that window loses focus. "
            "Stop with Ctrl+C."
        ),
    )
    run.add_argument(
        "profile", help=f"profile name (in {paths.short(profiles.profiles_dir())}) or file"
    )
    run.add_argument(
        "--dry-run", action="store_true", help="log keys instead of sending them; no focus needed"
    )
    run.add_argument(
        "--record",
        nargs="?",
        const=True,
        metavar="WAV",
        help="record the source audio (default: audio.wav in the run folder)",
    )
    run.add_argument(
        "--source",
        help="use this source instead of the profile's, e.g. file:session.wav to replay a "
        "recording (replays need --dry-run and run in audio time, faster than real time)",
    )
    run.add_argument(
        "--meter", action="store_true", help="print each sound's best similarity every second"
    )
    run.add_argument("--runs-dir", type=Path, help=f"where run folders go (default {runs_dir()})")
    run.set_defaults(func=cmd_run)

    sub.add_parser("profiles", help="list saved profiles").set_defaults(func=cmd_profiles)

    doctor = sub.add_parser(
        "doctor",
        help="check the setup and print a report to paste when asking for help",
        description="Print versions, applications playing audio, profiles and, for PROFILE, "
        "whether its source plays, its window is open and focused, what each key is sent as "
        "and each sound's clips and settings. Sends no keys and writes no files.",
    )
    doctor.add_argument("profile", nargs="?", help="profile name or file to check")
    doctor.set_defaults(func=cmd_doctor)

    gui = sub.add_parser("gui", help="open the window")
    gui.add_argument("profile", nargs="?", help="profile to open")
    gui.set_defaults(func=cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":  # a console or pipe code page cannot print every character
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(errors="replace")  # pyright: ignore[reportAttributeAccessIssue]
    args = build_parser().parse_args(argv)
    return args.func(args)
