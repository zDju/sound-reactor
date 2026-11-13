"""Where sound-reactor keeps its files, per platform.

========  ===========================================  ==================================
          Linux (and other Unix)                       Windows
========  ===========================================  ==================================
config    ``$XDG_CONFIG_HOME/sound-reactor``           ``%APPDATA%\\sound-reactor``
          (``~/.config/sound-reactor``)                (roams with the user)
state     ``$XDG_STATE_HOME/sound-reactor``            ``%LOCALAPPDATA%\\sound-reactor``
          (``~/.local/state/sound-reactor``)           (this computer only)
========  ===========================================  ==================================

Profiles (``profiles/``) and the window's settings (``gui.conf``) are configuration; run folders
(``runs/``, with logs and possibly recordings) are state. The sounds themselves live in the
detector library's own folder.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

APP = "sound-reactor"


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def config_home(platform: str | None = None, environ: Mapping[str, str] | None = None) -> Path:
    env = _env(environ)
    if (platform or sys.platform) == "win32":
        base = env.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    else:
        base = env.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP


def state_home(platform: str | None = None, environ: Mapping[str, str] | None = None) -> Path:
    env = _env(environ)
    if (platform or sys.platform) == "win32":
        base = env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        base = env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / APP


def profiles_dir() -> Path:
    return config_home() / "profiles"


def runs_dir() -> Path:
    return state_home() / "runs"


def gui_settings_file() -> Path:
    """The window's settings (last profile, size), an INI file."""
    return config_home() / "gui.conf"


def short_text(text: str, home: Path | str | None = None) -> str:
    """``text`` with every mention of the home folder written as ``~`` (paths in messages, file:
    sources, URLs), with either kind of slash; any case on Windows."""
    folder = str(home if home is not None else Path.home()).rstrip("\\/")
    if not folder:
        return text
    flags = re.IGNORECASE if sys.platform == "win32" else 0
    for spelling in sorted({folder, folder.replace("\\", "/")}, key=len, reverse=True):
        pattern = r"(?<![\w.\\-])" + re.escape(spelling) + r"(?=[\\/]|$|[^\w.-])"
        text = re.sub(pattern, "~", text, flags=flags)
    return text


def short(path: Path | str) -> str:
    """``path`` with the home folder written as ``~`` (for output that gets pasted elsewhere)."""
    text, home = str(path), str(Path.home())
    if text == home or text.startswith(home.rstrip("\\/") + os.sep):
        return "~" + text[len(home.rstrip("\\/")) :]
    return text
