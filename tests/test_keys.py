"""The portable key names, and that every profile keeps loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from sound_reactor import keys
from sound_reactor import profile as profiles

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_portable_names():
    for name in ("a", "F", "z", "0", "9", "F1", "F12", "space", "Return", "Escape", "Tab",
                 "Page_Up", "Prior", "Next", "Up", "Left", "KP_Add", "KP_Enter", "minus",
                 "bracketleft", "backslash"):  # fmt: skip
        assert keys.is_portable(name), name
    for name in ("F13", "f1", "return", "exclam", "plus", "KP_0", "KP_Home", "Shift_L", "ctrl+f",
                 "", "ff"):  # fmt: skip
        assert not keys.is_portable(name), name
    assert keys.character("F") == keys.character("f") == "f"
    assert keys.character("bracketleft") == "[" and keys.character("F1") is None


def test_groups_cover_the_portable_set():
    listed = {name for _, _, names in keys.GROUPS for name in names}
    assert listed == keys.PORTABLE
    groups = [group for group, _, _ in keys.GROUPS]
    assert groups == ["letters", "digits", "function keys", "named keys", "arrows", "numpad",
                      "punctuation"]  # fmt: skip


def test_help_text():
    text = keys.help_text()
    assert "KEY (or key KEY), or wait SECONDS" in text and "separated by commas" in text
    shown = ("letters: a-z", "digits: 0-9", "F1-F12", "Return", "Page_Down",
             "arrows: Up, Down, Left, Right", "KP_Enter", "bracketleft [", "minus -")  # fmt: skip
    for part in shown:
        assert part in text, part
    assert "need Shift" in text and "combinations" in text
    assert "Linux only" not in text and "Linux only" in keys.help_text(linux_note=True)


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.toml")), ids=lambda p: p.name)
def test_example_profiles_use_portable_keys(example):
    p = profiles.load(example)
    assert all(keys.is_portable(k) for k in p.key_names())


def test_profiles_with_other_key_names_still_load():
    p = profiles.loads('[on_start]\nactions = [{ key = "KP_Home" }, { key = "exclam" }]\n')
    assert p.key_names() == ["KP_Home", "exclam"]  # checked when a run is prepared, as before
