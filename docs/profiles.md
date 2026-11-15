# Profiles

A profile is a TOML file in the config folder's `profiles/` (see
[Files and folders](usage.md#files-and-folders)); the window edits and saves the same files.

## Example

`examples/wow-fishing.toml` (`examples/wow-fishing-windows.toml` is the same for Windows, with
the game's executable to fill in and a 3-cast limit for a first test):

```toml
source = "app:World of Warcraft"   # app:NAME, device:TEXT, default, or file:PATH.wav
target = "World of Warcraft"       # title of the window that receives the keys
ignore_after_action = 1.5          # sounds starting this soon after a key are ignored
start_delay = 5.0

[sounds.bite]                      # an event of the detector library; overrides optional
min_level_db = -40.0

[on_start]
actions = [{ key = "9" }]

[[on_sound]]
sound = "bite"
actions = [{ wait = 0.25 }, { key = "f" }, { wait = 1.0 }, { key = "9" }]

[on_silence]                       # nothing heard in this long: these actions, new cycle
seconds = 30.0
actions = [{ key = "9" }]

[stop]
max_minutes = 60.0
max_silent_in_row = 5
max_cycles = 100
```

The sounds are events in the detector library (`sounddetector add ...`, see the
[detector's quick start](https://github.com/zDju/sound-event-detector#quick-start)).

## Cycles and rules

A cycle starts when an action list ends. The first sound with a rule in a cycle runs that
rule's actions; sounds heard while actions run, or starting within `ignore_after_action` of a
key, are ignored.

## Key names

Keys are named as X names them, and these names work on both platforms
(`sound_reactor/keys.py`):

- `a`–`z`, `0`–`9`, `F1`–`F12`;
- `space`, `Return`, `Escape`, `Tab`, `BackSpace`, `Delete`, `Insert`, `Home`, `End`,
  `Page_Up`, `Page_Down` (`Prior`, `Next`), `Up`, `Down`, `Left`, `Right`;
- `KP_Add`, `KP_Subtract`, `KP_Multiply`, `KP_Divide`, `KP_Enter`;
- `minus`, `equal`, `comma`, `period`, `slash`, `semicolon`, `apostrophe`, `grave`,
  `bracketleft`, `bracketright`, `backslash`.

On Linux any other X key name works too. Each key is pressed alone, without modifiers: a live
run refuses a name that needs Shift or AltGr on the current layout (`exclam`, `plus`, `9` on
AZERTY), since only the plain key would be sent.
