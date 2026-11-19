"""Profiles: what to listen to, which window gets the keys, and which actions answer which sound.

A profile is a TOML file, by default in ``~/.config/sound-reactor/profiles/<name>.toml``
(``%APPDATA%\\sound-reactor\\profiles`` on Windows, see :mod:`.paths`)::

    source = "app:World of Warcraft"     # detector source: app:NAME, device:TEXT, file:PATH.wav
    target = "World of Warcraft"         # title of the window that receives keys

    [sounds.bite]                         # detector event from the library; optional overrides
    min_level_db = -55

    [on_start]
    actions = [{ key = "9" }]

    [[on_sound]]
    sound = "bite"
    actions = [{ wait = 0.25 }, { key = "f" }, { wait = 1 }, { key = "9" }]

    [on_silence]
    seconds = 30
    actions = [{ key = "9" }]

    [stop]
    max_minutes = 60
    max_silent_in_row = 5
    max_cycles = 100

Parsing checks types and names; whether a profile can actually run (its sounds exist, its keys
are known, its target window is open) is checked when a run is prepared.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sound_event_detector.library import SETTINGS as SOUND_SETTINGS

from . import paths

DEFAULT_IGNORE_AFTER_ACTION = 1.5  # s: sounds starting this soon after a key are ignored
DEFAULT_START_DELAY = 5.0  # s between switching into the target window and the first action
DEFAULT_SILENCE = 30.0  # s

# Allowed ranges of the sound settings (low, high, low inclusive); the rest must be >= 0.
SETTING_RANGES = {
    "threshold": (0.0, 1.0, False),
    "min_level_db": (-math.inf, 0.0, True),
    "pitch_tolerance": (0.0, 0.5, True),
    "template_length": (0.0, 10.0, True),
}
# What a value means, added to range errors.
SETTING_UNITS = {
    "pitch_tolerance": "a fraction of the speed: 0.06 is +/-6 %",
    "template_length": "seconds of each clip; 0 = the whole clip",
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")  # as the detector library's event names
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # TOML keys that need no quotes


class ProfileError(ValueError):
    """The profile file is malformed or has values of the wrong kind."""


@dataclass(frozen=True)
class Key:
    """Press and release one key (``9``, ``f``, ``F1``, ``space``; see :mod:`.keys`)."""

    key: str


@dataclass(frozen=True)
class Wait:
    seconds: float


Action = Key | Wait


@dataclass
class SoundRule:
    sound: str
    actions: list[Action] = field(default_factory=list)


@dataclass
class SilenceRule:
    seconds: float = DEFAULT_SILENCE
    actions: list[Action] = field(default_factory=list)


@dataclass
class StopRules:
    """Limits of a run; ``None`` means no limit."""

    max_minutes: float | None = None
    max_silent_in_row: int | None = None
    max_cycles: int | None = None


@dataclass
class Profile:
    source: str = ""
    target: str = ""
    sounds: dict[str, dict[str, float]] = field(default_factory=dict)  # name -> overrides
    on_start: list[Action] = field(default_factory=list)
    on_sound: list[SoundRule] = field(default_factory=list)
    on_silence: SilenceRule | None = None
    stop: StopRules = field(default_factory=StopRules)
    ignore_after_action: float = DEFAULT_IGNORE_AFTER_ACTION
    start_delay: float = DEFAULT_START_DELAY

    def sound_names(self) -> list[str]:
        """The sounds to listen for: those under ``[sounds]``, then those only named by a rule."""
        names = list(self.sounds)
        names += [r.sound for r in self.on_sound if r.sound not in names]
        return names

    def key_names(self) -> list[str]:
        """Every key the profile can press, once each."""
        lists = [self.on_start, *(r.actions for r in self.on_sound)]
        if self.on_silence is not None:
            lists.append(self.on_silence.actions)
        seen: dict[str, None] = {}
        for actions in lists:
            seen.update((a.key, None) for a in actions if isinstance(a, Key))
        return list(seen)


# -- parsing -----------------------------------------------------------------------------------


def _known(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ProfileError(f"{where}: unknown setting(s) {sorted(unknown)}")


def _table(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{where} must be a table")
    return value


def _number(value: object, where: str, *, minimum: float = 0.0, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ProfileError(f"{where} must be a number")
    if value < minimum or (strict and value == minimum):
        raise ProfileError(f"{where} must be {'>' if strict else '>='} {minimum:g}")
    return float(value)


def _setting(key: str, value: object, where: str) -> float:
    low, high, low_ok = SETTING_RANGES.get(key, (0.0, math.inf, True))
    unit = f" ({SETTING_UNITS[key]})" if key in SETTING_UNITS else ""
    try:
        number = _number(value, where, minimum=low, strict=not low_ok)
    except ProfileError as e:
        raise ProfileError(f"{e}{unit}") from None
    if number > high:
        raise ProfileError(f"{where} must be <= {high:g}{unit}")
    return number


def _count(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProfileError(f"{where} must be a whole number >= 1")
    return value


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{where} must be a string")
    return value


def _sound_name(value: object, where: str) -> str:
    name = _string(value, where)
    if not NAME_RE.match(name):
        raise ProfileError(f"{where}: invalid sound name {name!r}")
    return name


def _key_name(value: object, where: str) -> str:
    key = _string(value, where).strip()
    if not key or any(c.isspace() for c in key) or "," in key:
        raise ProfileError(f"{where}: invalid key name {value!r}")
    return key


def _actions(value: object, where: str) -> list[Action]:
    if not isinstance(value, list):
        raise ProfileError(f'{where} must be a list such as [{{ key = "f" }}, {{ wait = 1 }}]')
    actions: list[Action] = []
    for i, item in enumerate(value, 1):
        here = f"{where}, step {i}"
        step = _table(item, here)
        if set(step) == {"key"}:
            actions.append(Key(_key_name(step["key"], here)))
        elif set(step) == {"wait"}:
            actions.append(Wait(_number(step["wait"], f"{here}: wait")))
        else:
            raise ProfileError(f'{here} must be {{ key = "..." }} or {{ wait = SECONDS }}')
    return actions


def from_dict(data: dict[str, Any]) -> Profile:
    """A profile from parsed TOML; raises :class:`ProfileError` with the offending setting."""
    _known(
        data,
        {"source", "target", "ignore_after_action", "start_delay", "sounds", "on_start",
         "on_sound", "on_silence", "stop"},
        "profile",
    )  # fmt: skip
    p = Profile(
        source=_string(data.get("source", ""), "source"),
        target=_string(data.get("target", ""), "target"),
        ignore_after_action=_number(
            data.get("ignore_after_action", DEFAULT_IGNORE_AFTER_ACTION), "ignore_after_action"
        ),
        start_delay=_number(data.get("start_delay", DEFAULT_START_DELAY), "start_delay"),
    )
    for name, overrides in _table(data.get("sounds", {}), "[sounds]").items():
        where = f"[sounds.{name}]"
        _sound_name(name, where)
        table = _table(overrides, where)
        _known(table, set(SOUND_SETTINGS), where)
        p.sounds[name] = {k: _setting(k, v, f"{where} {k}") for k, v in table.items()}
    if "on_start" in data:
        table = _table(data["on_start"], "[on_start]")
        _known(table, {"actions"}, "[on_start]")
        p.on_start = _actions(table.get("actions", []), "[on_start] actions")
    rules = data.get("on_sound", [])
    if not isinstance(rules, list):
        raise ProfileError("on_sound must be an array of tables ([[on_sound]])")
    for i, rule in enumerate(rules, 1):
        where = f"[[on_sound]] #{i}"
        table = _table(rule, where)
        _known(table, {"sound", "actions"}, where)
        if "sound" not in table:
            raise ProfileError(f"{where}: missing 'sound'")
        sound = _sound_name(table["sound"], f"{where} sound")
        if any(r.sound == sound for r in p.on_sound):
            raise ProfileError(f"{where}: a rule for {sound!r} already exists")
        p.on_sound.append(SoundRule(sound, _actions(table.get("actions", []), f"{where} actions")))
    if "on_silence" in data:
        table = _table(data["on_silence"], "[on_silence]")
        _known(table, {"seconds", "actions"}, "[on_silence]")
        p.on_silence = SilenceRule(
            _number(table.get("seconds", DEFAULT_SILENCE), "[on_silence] seconds", strict=True),
            _actions(table.get("actions", []), "[on_silence] actions"),
        )
    stop = _table(data.get("stop", {}), "[stop]")
    _known(stop, {"max_minutes", "max_silent_in_row", "max_cycles"}, "[stop]")
    p.stop = StopRules(
        max_minutes=None
        if "max_minutes" not in stop
        else _number(stop["max_minutes"], "[stop] max_minutes", strict=True),
        max_silent_in_row=None
        if "max_silent_in_row" not in stop
        else _count(stop["max_silent_in_row"], "[stop] max_silent_in_row"),
        max_cycles=None
        if "max_cycles" not in stop
        else _count(stop["max_cycles"], "[stop] max_cycles"),
    )
    return p


def loads(text: str) -> Profile:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"not valid TOML: {e}") from e
    return from_dict(data)


def load(path: str | Path) -> Profile:
    path = Path(path)
    try:
        # utf-8-sig: Windows PowerShell 5.1 (Set-Content -Encoding utf8) writes a byte order mark
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ProfileError(f"cannot read {path}: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise ProfileError(f"{path} is not a UTF-8 text file: {e}") from e
    try:
        return loads(text)
    except ProfileError as e:
        raise ProfileError(f"{path}: {e}") from e


# -- writing -----------------------------------------------------------------------------------


def _str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings


def _num(v: float | int) -> str:
    return repr(v)


def _toml_key(name: str) -> str:
    return name if BARE_KEY_RE.match(name) else _str(name)


def _inline_actions(actions: list[Action]) -> str:
    steps = [
        f"{{ key = {_str(a.key)} }}" if isinstance(a, Key) else f"{{ wait = {_num(a.seconds)} }}"
        for a in actions
    ]
    return f"[{', '.join(steps)}]"


def dumps(p: Profile) -> str:
    lines = [
        f"source = {_str(p.source)}",
        f"target = {_str(p.target)}",
        f"ignore_after_action = {_num(p.ignore_after_action)}",
        f"start_delay = {_num(p.start_delay)}",
    ]
    for name, overrides in p.sounds.items():
        lines += ["", f"[sounds.{_toml_key(name)}]"]
        lines += [f"{k} = {_num(v)}" for k, v in overrides.items()]
    lines += ["", "[on_start]", f"actions = {_inline_actions(p.on_start)}"]
    for rule in p.on_sound:
        lines += [
            "",
            "[[on_sound]]",
            f"sound = {_str(rule.sound)}",
            f"actions = {_inline_actions(rule.actions)}",
        ]
    if p.on_silence is not None:
        lines += [
            "",
            "[on_silence]",
            f"seconds = {_num(p.on_silence.seconds)}",
            f"actions = {_inline_actions(p.on_silence.actions)}",
        ]
    limits = [(k, getattr(p.stop, k)) for k in ("max_minutes", "max_silent_in_row", "max_cycles")]
    lines += ["", "[stop]"] + [f"{k} = {_num(v)}" for k, v in limits if v is not None]
    return "\n".join(lines) + "\n"


def save(p: Profile, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps(p), encoding="utf-8")
    tmp.replace(path)
    return path


# -- actions as text (the GUI's rule editor) ---------------------------------------------------


def format_actions(actions: list[Action]) -> str:
    """``[Wait(0.25), Key("f")]`` -> ``"wait 0.25, f"``."""
    return ", ".join(
        a.key if isinstance(a, Key) else f"wait {_seconds(a.seconds)}" for a in actions
    )


def _seconds(value: float) -> str:
    """The shortest text that reads back as exactly ``value`` (``2.5``, ``30``, ``0.123456789``)."""
    text = repr(float(value))
    return text.removesuffix(".0")


def parse_actions(text: str) -> list[Action]:
    """The inverse of :func:`format_actions`: steps separated by commas, each ``wait SECONDS``,
    ``key NAME`` or just a key name."""
    actions: list[Action] = []
    for i, part in enumerate(text.split(","), 1):
        words = part.split()
        if not words:
            continue
        where = f"step {i} ({part.strip()!r})"
        if words[0] == "wait" and len(words) == 2:
            try:
                seconds = float(words[1])
            except ValueError:
                raise ProfileError(f"{where}: 'wait' needs a number of seconds") from None
            actions.append(Wait(_number(seconds, where)))
        elif words[0] == "key" and len(words) == 2:
            actions.append(Key(_key_name(words[1], where)))
        elif len(words) == 1 and words[0] != "wait":
            actions.append(Key(_key_name(words[0], where)))
        else:
            raise ProfileError(f"{where}: use 'wait SECONDS' or a key name")
    return actions


# -- where profiles live -----------------------------------------------------------------------


def config_home() -> Path:
    return paths.config_home()


def profiles_dir() -> Path:
    return paths.profiles_dir()


def list_profiles() -> list[str]:
    folder = profiles_dir()
    return sorted(p.stem for p in folder.glob("*.toml")) if folder.is_dir() else []


def resolve(name_or_path: str) -> Path:
    """A profile file from a name in :func:`profiles_dir` or a path."""
    path = Path(name_or_path).expanduser()
    separators = {os.sep, os.altsep} - {None}  # "/" on Linux; "\\" and "/" on Windows
    if path.suffix == ".toml" or path.is_file() or any(s in name_or_path for s in separators):
        return path
    return profiles_dir() / f"{name_or_path}.toml"
