"""Where files go on each platform; on Linux exactly where they always went."""

from __future__ import annotations

from pathlib import Path

from sound_reactor import paths, runner
from sound_reactor import profile as profiles

HOME = Path.home()


def test_linux_folders_are_unchanged():
    assert paths.config_home("linux", {}) == HOME / ".config" / "sound-reactor"
    assert paths.state_home("linux", {}) == HOME / ".local" / "state" / "sound-reactor"
    env = {"XDG_CONFIG_HOME": "/x/config", "XDG_STATE_HOME": "/x/state", "APPDATA": "/no"}
    assert paths.config_home("linux", env) == Path("/x/config/sound-reactor")
    assert paths.state_home("linux", env) == Path("/x/state/sound-reactor")
    empty = {"XDG_CONFIG_HOME": "", "XDG_STATE_HOME": ""}  # empty counts as unset
    assert paths.config_home("linux", empty) == HOME / ".config" / "sound-reactor"


def test_windows_folders():
    env = {
        "APPDATA": "C:/Users/me/AppData/Roaming",
        "LOCALAPPDATA": "C:/Users/me/AppData/Local",
        "XDG_CONFIG_HOME": "/ignored",
    }
    assert paths.config_home("win32", env) == Path("C:/Users/me/AppData/Roaming/sound-reactor")
    assert paths.state_home("win32", env) == Path("C:/Users/me/AppData/Local/sound-reactor")
    assert paths.config_home("win32", {}) == HOME / "AppData" / "Roaming" / "sound-reactor"
    assert paths.state_home("win32", {}) == HOME / "AppData" / "Local" / "sound-reactor"


def test_everything_follows_the_platform_folders(isolated_homes):
    config, state = isolated_homes / "config" / "sound-reactor", isolated_homes / "state"
    assert paths.profiles_dir() == profiles.profiles_dir() == config / "profiles"
    assert paths.gui_settings_file() == config / "gui.conf"
    assert paths.runs_dir() == runner.runs_dir() == state / "sound-reactor" / "runs"
    assert profiles.resolve("wow") == config / "profiles" / "wow.toml"


def test_home_is_shortened_for_reports():
    assert paths.short(HOME / "a" / "b.toml") == str(Path("~") / "a" / "b.toml")
    assert paths.short(HOME) == "~"
    assert paths.short("/elsewhere/x") == "/elsewhere/x"
    assert paths.short(str(HOME) + "other") == str(HOME) + "other"  # not inside home


def test_home_is_shortened_inside_any_text():
    home = "/home/someone"
    text = (
        f"profile {home}/p.toml; source file:{home}/rec.wav; url file://{home}/x; "
        f"'{home}'; {home}2/other; /mnt{home}/backup"
    )
    assert paths.short_text(text, home=home) == (
        "profile ~/p.toml; source file:~/rec.wav; url file://~/x; "
        f"'~'; {home}2/other; /mnt{home}/backup"
    )
    windows = r"C:\Users\Name"
    assert paths.short_text(rf"{windows}\AppData and C:/Users/Name/x", home=windows) == (
        r"~\AppData and ~/x"
    )
