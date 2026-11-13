"""Key names in profiles, and the set that works on every platform.

Profiles name keys as X does (``9``, ``f``, ``F1``, ``space``, ``Return``), and they always will.
The **portable** names in :data:`GROUPS` work on Linux and on Windows; each desktop backend maps
them to its own codes, and the window's help text is made from the same table. A key is always
pressed alone, without modifiers: a name whose character needs Shift or AltGr on the current
keyboard layout is refused on both platforms, and combinations (Ctrl+F) cannot be written.

On Linux any other X key name keeps working (``exclam`` is refused as before, ``KP_Home``
works); on Windows only the portable names are known. Numpad digits are left out: X11 treats
them as needing Num Lock, a modifier.
"""

from __future__ import annotations

LETTERS = tuple("abcdefghijklmnopqrstuvwxyz")  # A-Z too: F and f are the same key
DIGITS = tuple("0123456789")
FUNCTION_KEYS = tuple(f"F{n}" for n in range(1, 13))
NAMED_KEYS = (
    "space", "Return", "Escape", "Tab", "BackSpace", "Delete", "Insert", "Home", "End",
    "Page_Up", "Page_Down",
)  # fmt: skip
ALIASES = {"Prior": "Page_Up", "Next": "Page_Down"}  # other X names of the same keys
ARROWS = ("Up", "Down", "Left", "Right")
NUMPAD = ("KP_Add", "KP_Subtract", "KP_Multiply", "KP_Divide", "KP_Enter")
# Keys that type a character: which key that is depends on the keyboard layout.
PUNCTUATION = {
    "minus": "-", "equal": "=", "comma": ",", "period": ".", "slash": "/", "semicolon": ";",
    "apostrophe": "'", "grave": "`", "bracketleft": "[", "bracketright": "]", "backslash": "\\",
}  # fmt: skip

# (group, how the group is shown, its names): what the window lists, and what is portable
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("letters", "a-z", LETTERS),
    ("digits", "0-9", DIGITS),
    ("function keys", "F1-F12", FUNCTION_KEYS),
    ("named keys", ", ".join(NAMED_KEYS), (*NAMED_KEYS, *ALIASES)),
    ("arrows", ", ".join(ARROWS), ARROWS),
    ("numpad", ", ".join(NUMPAD), NUMPAD),
    ("punctuation", ", ".join(f"{n} {c}" for n, c in PUNCTUATION.items()), tuple(PUNCTUATION)),
)
PORTABLE = frozenset(name for _, _, names in GROUPS for name in names)
PORTABLE_TEXT = "; ".join(f"{group}: {shown}" for group, shown, _ in GROUPS)  # for messages


def character(name: str) -> str | None:
    """The character a portable key name types (``F`` and ``f`` are the same key: ``"f"``), or
    None for keys that type none (``F1``, ``space``...) and for unknown names."""
    if len(name) == 1 and (name.lower() in LETTERS or name in DIGITS):
        return name.lower()
    return PUNCTUATION.get(name)


def is_portable(name: str) -> bool:
    return name in PORTABLE or character(name) is not None


def help_text(*, linux_note: bool = False) -> str:
    """How to write action steps and which keys there are (the window's tooltips)."""
    lines = [
        "Steps, separated by commas: KEY (or key KEY), or wait SECONDS.",
        "Example: wait 0.25, f, wait 2.5, 9",
        "",
        "Keys:",
        *(f"  {group}: {shown}" for group, shown, _ in GROUPS),
        "",
        "Each key is pressed alone: keys that need Shift or AltGr (such as ! or +) and",
        "combinations (such as Ctrl+F) are not supported.",
    ]
    if linux_note:
        lines.append("Other X key names work on Linux only.")
    return "\n".join(lines)


def describe_held(keys: list[str], buttons: list[int]) -> str | None:
    """``["w"], [1, 3]`` -> ``"key(s) w and mouse button(s) 1, 3"``; None if nothing is held.
    Mouse buttons are numbered as in X11: 1 left, 2 middle, 3 right, 8 and 9 the side buttons."""
    parts = []
    if keys:
        parts.append(f"key(s) {', '.join(keys)}")
    if buttons:
        parts.append(f"mouse button(s) {', '.join(map(str, buttons))}")
    return " and ".join(parts) or None
