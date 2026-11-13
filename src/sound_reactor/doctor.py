"""``sound-reactor doctor [PROFILE]``: plain text to paste into a chat when something does not
work. It reads and looks up; it never sends a key and never writes a file.

It shows the versions, the platform and its desktop backend, the folders, the applications
playing audio and the saved profiles; for a profile, whether its source is playing, whether its
target window is open and focused, what every key is sent as, whether Windows would drop the
keys (UIPI), and each sound's clips and settings. Everything that would stop a run is repeated
under "warnings". The text is ASCII and the home folder is written as ``~``.
"""

from __future__ import annotations

import json
import platform
import re
import sys
from collections.abc import Callable
from datetime import datetime
from importlib import metadata
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from sound_event_detector import Library
from sound_event_detector import list_apps as playing_apps
from sound_event_detector.capture import AppStream, find_app

from . import desktop, keys, paths
from . import profile as profiles
from .desktop import Backend
from .profile import Profile, ProfileError
from .runner import problems, replay_path

MIN_WINDOWS_BUILD = 19041  # Windows 10 2004: per-application capture (process loopback)
PLACEHOLDER = "REPLACE-ME"  # in the Windows example profile's source


def distribution(name: str) -> str:
    """``0.1.0 (git https://github.com/... @ d1cc0f6)``: the version and where it came from."""
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return "not installed"
    text = dist.version
    try:
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        origin = {}
    url = str(origin.get("url", ""))
    if vcs := origin.get("vcs_info"):
        text += f" (git {url} @ {str(vcs.get('commit_id', '?'))[:7]})"
    elif url.startswith("file:"):
        where = paths.short(url2pathname(unquote(urlparse(url).path)))
        editable = (origin.get("dir_info") or {}).get("editable")
        text += f" ({'editable, ' if editable else ''}{where})"
    elif archive := re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/archive/(.+)\.zip", url):
        repository, ref = archive.groups()
        ref = ref[:7] if re.fullmatch(r"[0-9a-f]{40}", ref) else ref
        text += f" (github {repository} @ {ref})"
    elif url:
        text += f" ({url})"
    return text


def qt_version() -> str:
    try:
        import PySide6
        from PySide6 import QtCore

        return f"{QtCore.qVersion()} (PySide6 {PySide6.__version__})"
    except Exception as e:
        return f"not available ({type(e).__name__}: {e})"


def app_details(a: AppStream) -> str:
    details = [f"pid {a.pid}" if a.pid is not None else "pid ?"]
    if a.binary and a.binary != a.name:
        details.append(a.binary)
    if a.state:
        details.append(a.state)
    return ", ".join(details)


def describe_app(a: AppStream) -> str:
    return f"app:{a.name}  ({app_details(a)})"


def describe_sound(settings: dict[str, float], overrides: dict[str, float]) -> str:
    def mark(key: str) -> str:
        return "*" if key in overrides else ""

    s = settings
    pitch = f"+/-{s['pitch_tolerance'] * 100:g} %" if s["pitch_tolerance"] else "off"
    length = f"{s['template_length']:g} s" if s["template_length"] else "whole clips"
    return (
        f"threshold {s['threshold']:g}{mark('threshold')}, "
        f"min level {s['min_level_db']:g} dBFS{mark('min_level_db')}, "
        f"pitch {pitch}{mark('pitch_tolerance')}, length {length}{mark('template_length')}, "
        f"cooldown {s['cooldown']:g} s{mark('cooldown')}"
    )


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self.blocking = 0  # warnings that would stop Start

    def out(self, line: str = "") -> None:
        self.lines.append(line)

    def warn(self, message: str, *, blocks: bool = False) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            self.blocking += blocks

    def block(self, message: str) -> None:
        self.warn(message, blocks=True)

    def text(self) -> str:
        self.out()
        self.out("warnings:" if self.warnings else "warnings: none")
        self.lines += [f"  - {w}" for w in self.warnings]
        text = paths.short_text("\n".join(self.lines))  # also in messages and file: sources
        return text.encode("ascii", "replace").decode("ascii") + "\n"


def report(
    profile: str | None = None,
    *,
    backend: Backend | None = None,
    list_apps: Callable[[], list[AppStream]] | None = None,
    library: Library | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> str:
    """The doctor's text. ``backend``, ``list_apps`` and ``library`` default to the real ones."""
    backend = backend or desktop.current()
    library = library or Library()
    r = Report()
    r.out(f"sound-reactor doctor, {now():%Y-%m-%d %H:%M}")
    r.out()
    r.out(f"sound-reactor {distribution('sound-reactor')}")
    r.out(f"sound-event-detector {distribution('sound-event-detector')}")
    r.out(f"Python {platform.python_version()}, Qt {qt_version()}")
    r.out(f"platform: {platform.platform()} ({platform.machine()})")
    if sys.platform == "win32":
        build = sys.getwindowsversion().build  # pyright: ignore[reportAttributeAccessIssue]
        if build < MIN_WINDOWS_BUILD:
            r.warn(
                f"Windows build {build}: capturing one application needs Windows 10 version "
                f"2004 (build {MIN_WINDOWS_BUILD}) or later"
            )
    r.out(f"desktop: {backend.name}, keys sent with {backend.sends_with}")
    try:
        about = backend.about()
    except Exception as e:
        about = [f"({type(e).__name__}: {e})"]
    r.lines += [f"  {line}" for line in about]
    r.out("folders:")
    r.out(f"  profiles: {paths.short(profiles.profiles_dir())}")
    r.out(f"  runs: {paths.short(paths.runs_dir())}")
    r.out(f"  sounds (detector library): {paths.short(library.root)}")

    r.out()
    apps: list[AppStream] | None
    try:
        apps = list((list_apps or playing_apps)())
    except Exception as e:
        apps = None
        r.out(f"applications playing audio: cannot list them ({type(e).__name__}: {e})")
        r.warn(f"cannot list the applications playing audio: {e}")
    else:
        r.out("applications playing audio (a profile's source is one of these):")
        r.lines += [f"  {describe_app(a)}" for a in apps] or ["  none right now"]

    names = profiles.list_profiles()
    r.out(f"profiles: {', '.join(names) if names else 'none'}")
    if profile is not None:
        r.out()
        _profile(r, profile, backend, apps, library)
    return r.text()


def _profile(
    r: Report, name: str, backend: Backend, apps: list[AppStream] | None, library: Library
) -> None:
    path = profiles.resolve(name)
    try:
        p = profiles.load(path)
    except ProfileError as e:
        r.out(f"profile {name}: cannot be read")
        r.block(str(e))
        return
    r.out(f"profile {path.stem} ({paths.short(path)}):")
    for message in problems(p, dry_run=False, backend=backend):
        r.block(message)
    _source(r, p, backend, apps)
    _target(r, p, backend)
    _sounds(r, p, library)
    if backend.name != "Windows":
        for key in p.key_names():
            if not keys.is_portable(key) and backend.key_error(key) is None:
                r.warn(f"key {key!r} is not one of the portable key names: it works on Linux only")
    n = r.blocking
    r.out(f"  start: {f'blocked by {n} problem(s), see the warnings' if n else 'ready'}")


def _source(r: Report, p: Profile, backend: Backend, apps: list[AppStream] | None) -> None:
    kind, _, value = p.source.partition(":")
    value = value.strip()
    if replay_path(p.source) is not None:
        r.out(f"  source {p.source}: a recording (replay it with Listen only)")
        return
    if kind != "app" or not value:
        r.out(f"  source {p.source or '(none)'}: not one application (it hears everything)")
        return
    if PLACEHOLDER in value:
        r.out(f"  source {p.source}: a placeholder")
        r.block(
            f"replace {p.source!r} in the profile with the game's entry from 'applications "
            "playing audio' above (e.g. app:Wow.exe)"
        )
        return
    if apps is None:
        r.out(f"  source {p.source}: cannot tell whether it is playing")
        return
    found = find_app(value, apps)
    if found is None:
        r.out(f"  source {p.source}: NOT playing audio now")
        r.block(f"nothing called {value!r} is playing audio now (start the game, let it play)")
        return
    r.out(f"  source {p.source}: playing audio ({app_details(found)})")
    if found.pid is not None:
        try:
            titles = backend.app_windows(found.pid)
        except Exception:
            titles = []
        if titles:
            r.out(f"    its windows: {', '.join(repr(t) for t in titles)}")


def _target(r: Report, p: Profile, backend: Backend) -> None:
    unchecked = (
        f"  keys: {', '.join(p.key_names()) or 'none'} (checked against the keyboard layout "
        "once the window is found)"
    )
    if not p.target.strip():
        r.out("  target: none")
        r.out(unchecked)
        return
    try:
        target = backend.find_target(p.target)
    except LookupError:
        r.out(f"  target {p.target!r}: NO such window is open")
        r.block(f"no window titled {p.target!r} is open")
        similar = _similar(p.target, backend)
        if similar:
            r.out(f"    similar titles: {', '.join(repr(t) for t in similar)}")
        r.out(unchecked)
        return
    except Exception as e:
        r.out(f"  target {p.target!r}: cannot look ({type(e).__name__}: {e})")
        r.block(f"cannot reach {backend.reach}: {type(e).__name__}: {e}")
        r.out(unchecked)
        return
    try:
        focused = "yes" if target.focused() else "no (as expected while this runs in front)"
        r.out(f"  target {p.target!r}: window found, focused: {focused}")
        r.lines += [f"    {line}" for line in backend.target_about(target)]
        r.out(f"  keys (each sent alone, with {backend.sends_with}):")
        for key in p.key_names():
            try:
                r.out(f"    {key}: {target.describe_key(key)}")
            except ValueError as e:
                r.out(f"    {key}: REFUSED")
                r.block(str(e))
        found = backend.target_problems(target)
        if backend.name == "Windows":
            r.out(f"  administrator rights: {'MISMATCH' if found else 'OK (same level)'}")
        for message in found:
            r.block(message)
    finally:
        target.close()


def _similar(title: str, backend: Backend) -> list[str]:
    """Open windows whose title contains the target's or is contained in it (any case)."""
    wanted = title.strip().casefold()
    try:
        titles = backend.window_titles()
    except Exception:
        return []
    return [t for t in titles if t.strip() and (wanted in t.casefold() or t.casefold() in wanted)]


def _sounds(r: Report, p: Profile, library: Library) -> None:
    r.out("  sounds (* = set in the profile):" if p.sound_names() else "  sounds: none")
    for name in p.sound_names():
        overrides = p.sounds.get(name, {})
        try:
            info = library.info(name)
        except KeyError:
            r.out(f"    {name}: NO clips in the detector library")
            r.block(f"the sound {name!r} has no clips in the detector library")
            continue
        except ValueError as e:
            r.out(f"    {name}: cannot be read ({e})")
            r.block(f"the sound {name!r} cannot be read: {e}")
            continue
        count = len(info.clips)
        clips = f"{count} clip{'s' if count != 1 else ''}"
        r.out(f"    {name}: {clips}; {describe_sound(info.settings | overrides, overrides)}")


def main_text(profile: str | None) -> tuple[str, int]:
    """The report and the exit code (2: the profile cannot be read)."""
    text = report(profile)
    code = 0
    if profile is not None:
        try:
            profiles.load(profiles.resolve(profile))
        except ProfileError:
            code = 2
    return text, code
