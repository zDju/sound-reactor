# sound-reactor

Hear a sound, press keys: `sound-reactor` listens for sound events with
[sound-event-detector](https://github.com/zDju/sound-event-detector) and answers each with a
list of key presses and waits, sent to one window. Its first use is a test: fishing in World of
Warcraft on Linux (cast, hear the bite, interact, recast). See `PLAN.md` for what was measured
and why.

> **Platform status.** Used on Linux (X11 or XWayland). Windows 10 2004+ / 11 is implemented
> but **unverified on real hardware**; testers follow [docs/windows-testing.md](docs/windows-testing.md).

> Automating a game can break its Terms of Use. Detection evasion is out of scope: no random
> delays, no memory reading, no injection.

## Install

```bash
uv tool install --no-sources https://github.com/zDju/sound-reactor/archive/refs/heads/main.zip --with-executables-from sound-event-detector
```

This installs `sound-reactor` and the detector's `sounddetector` command into one environment,
both from GitHub zip archives, so Git is not needed: this repository's `main`, and the detector
at the commit pinned in `pyproject.toml`. `--no-sources` is required: `pyproject.toml` points uv
at `../sound-event-detector` for development, which does not exist in an install. Run the same
command with `--reinstall` to update. (`pip install` of the same URL needs no flag.)

**Linux**: X11 or XWayland (the target must be an X11 window) and PipeWire for capturing one
application's audio. **Windows 10 2004+ / 11**: nothing else; keys are sent with `SendInput` as
scan codes, to the target only while it is the foreground window, and the game and
sound-reactor must run at the same privilege level (not one of them "as administrator"), which
is checked before a start. The sounds are events in the detector library
(`sounddetector add ...`, see its README).

## Usage

```bash
uv run sound-reactor doctor wow-fishing               # check everything; paste this when asking
uv run sound-reactor profiles                         # profiles in the config folder (below)
uv run sound-reactor run wow-fishing --dry-run        # everything but the keys; no focus needed
uv run sound-reactor run wow-fishing                  # live: switch into the game to start
uv run sound-reactor run wow-fishing --record         # also record the source audio
uv run sound-reactor run wow-fishing --dry-run --source file:session.wav   # replay a recording
uv run sound-reactor gui [wow-fishing]                # the window
```

A **live run** only arms at first: it starts when you switch into the target window
(Alt+Tab), then waits the profile's start delay (at least 5 s). It stops when that window
loses focus, the audio disconnects, a key or mouse button is held before a key would be sent, a
limit is reached, or on Ctrl+C, SIGTERM, SIGHUP (Linux), Ctrl+Break (Windows) or Stop. On
Windows, closing the console window it runs from stops it too: Windows ends the process once
told, so the run gets up to 4 s to release its key and write its log and counters.

Keys go to the target window only. On Linux they are sent with XSendEvent; those do not count as
user activity, so a live run keeps the screen from locking
(`org.freedesktop.ScreenSaver.Inhibit`), since a screen lock would take focus. On Windows they
are sent with SendInput, which reaches the foreground window, so focus is checked again right
before each key and after it; SendInput counts as user activity, and a live run also keeps the
display on (`SetThreadExecutionState`, which does not stop a screen saver).

`sound-reactor doctor [PROFILE]` prints versions, the applications playing audio and the
profiles; for a profile also whether its source plays, whether its window is open and focused,
what each key is sent as, the administrator check (Windows) and each sound's clips and
settings, then every warning. It sends nothing and writes nothing.

**The window** (`sound-reactor gui`) has the profile on the left and the run on the right: a
coloured status line, *Start* (live), *Listen only* (dry run) and *Stop*, what would block a
start, counters, a meter per sound with a marker at its threshold, and the log. Threshold, min
level, pitch tolerance and clip length changed in the sounds table are saved in the profile;
otherwise the library's values apply. *Use* adds a library sound, *New sound…* and *Add clips…* cut WAV
recordings into clips, and a rule's sound is chosen from a list (double-click); the action fields' tooltips list the key names. It remembers
the last profile and its size (`gui.conf` in the config folder).

A **dry run** logs the keys instead of sending them. A replay (`file:` source, dry run only)
runs in audio time, faster than real time: waits and timeouts are seconds of the recording, and
log times are positions in it.

| | Linux | Windows |
|---|---|---|
| config: `profiles/`, `gui.conf` | `~/.config/sound-reactor` (`$XDG_CONFIG_HOME`) | `%APPDATA%\sound-reactor` |
| state: `runs/` | `~/.local/state/sound-reactor` (`$XDG_STATE_HOME`) | `%LOCALAPPDATA%\sound-reactor` |

Each run writes a folder in `runs/` (state) with `log.txt`, `counters.json`, the profile used
and, with `--record`, `audio.wav` (for `sounddetector scan`; `--record FILE` refuses a file that
already exists).
`--meter` prints each sound's best similarity and the source level every second.

## Profiles

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
actions = [{ wait = 0.25 }, { key = "f" }, { wait = 2.5 }, { key = "9" }]

[on_silence]                       # nothing heard in this long: these actions, new cycle
seconds = 30.0
actions = [{ key = "9" }]

[stop]
max_minutes = 60.0
max_silent_in_row = 5
max_cycles = 100
```

A cycle starts when an action list ends. The first sound with a rule in a cycle runs that
rule's actions; sounds heard while actions run, or starting within `ignore_after_action` of a
key, are ignored. Keys are named as X names them, and these names work on both platforms:
`a`–`z`, `0`–`9`, `F1`–`F12`, `space`, `Return`, `Escape`, `Tab`, `BackSpace`, `Delete`,
`Insert`, `Home`, `End`, `Page_Up`, `Page_Down` (`Prior`, `Next`), `Up`, `Down`, `Left`,
`Right`, `KP_Add`, `KP_Subtract`, `KP_Multiply`, `KP_Divide`, `KP_Enter`, and `minus`, `equal`,
`comma`, `period`, `slash`, `semicolon`, `apostrophe`, `grave`, `bracketleft`, `bracketright`,
`backslash` (`sound_reactor/keys.py`). On Linux any other X key name works too. Each key is
pressed alone, without modifiers: a live run refuses a name that needs Shift or AltGr on the
current layout (`exclam`, `plus`, `9` on AZERTY), since only the plain key would be sent.

## Development

```bash
uv run pytest            # fakes and synthetic audio; the GUI test runs offscreen
uv run pytest -m local   # replays a local recording (not in the repository)
uv run ruff check src tests && uv run ruff format --check src tests
```

`uv sync` takes the detector from `../sound-event-detector` (editable), so a clone needs it next
to this folder; CI (`.github/workflows/ci.yml`, Ubuntu and Windows) runs `uv sync --no-sources`
and gets it from GitHub. The Windows backend (`sound_reactor/win32.py`, ctypes) is tested on every
platform against a simulated Windows (`tests/winfakes.py`); a few tests use the real Windows
calls, on Windows only, and no test ever sends a real key.

`spikes/` holds the experiments that led here and is kept as they were
(`uv run --group spikes python spikes/input_test.py` for the one that needs `evdev`).
