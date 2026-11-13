# sound-reactor

Hear a sound, press keys: `sound-reactor` listens for sound events with
[sound-event-detector](https://github.com/zDju/sound-event-detector) and answers each with a
list of key presses and waits, sent to one X11 window. Its first use is a test: fishing in
World of Warcraft on Linux (cast, hear the bite, interact, recast). See `PLAN.md` for what was
measured and why.

> Automating a game can break its Terms of Use. Detection evasion is out of scope: no random
> delays, no memory reading, no injection.

## Usage

Requires Linux with X11 or XWayland (the target must be an X11 window), PipeWire for capturing
one application's audio, and the sounds as events in the detector library
(`sounddetector add ...`, see its README).

```bash
uv sync
uv run sound-reactor profiles                         # profiles in ~/.config/sound-reactor/profiles
uv run sound-reactor run wow-fishing --dry-run        # everything but the keys; no focus needed
uv run sound-reactor run wow-fishing                  # live: switch into the game to start
uv run sound-reactor run wow-fishing --record         # also record the source audio
uv run sound-reactor run wow-fishing --dry-run --source file:session.wav   # replay a recording
uv run sound-reactor gui [wow-fishing]                # the window
```

A **live run** only arms at first: it starts when you switch into the target window
(Alt+Tab), then waits the profile's start delay (at least 5 s). It stops when that window
loses focus, the audio disconnects, a key or mouse button is held before a key would be sent, a
limit is reached, or on Ctrl+C, SIGTERM, SIGHUP or Stop. Keys go to the target window only (XSendEvent). While it runs, it
keeps the screen from locking (`org.freedesktop.ScreenSaver.Inhibit`), because sent keys do not
count as user activity and a screen lock would take focus.

**The window** (`sound-reactor gui`) has the profile on the left and the run on the right: a
coloured status line, *Start* (live), *Try without keys* (dry run) and *Stop*, what would block a
start, counters, a meter per sound with a marker at its threshold, and the log. Threshold and
min level changed in the sounds table are saved in the profile; otherwise the library's values
apply. It remembers the last profile and its size (`~/.config/sound-reactor/gui.conf`).

A **dry run** logs the keys instead of sending them. A replay (`file:` source, dry run only)
runs in audio time, faster than real time: waits and timeouts are seconds of the recording, and
log times are positions in it.

Each run writes a folder in `~/.local/state/sound-reactor/runs/` with `log.txt`,
`counters.json`, the profile used and, with `--record`, `audio.wav` (for `sounddetector scan`;
`--record FILE` refuses a file that already exists).
`--meter` prints each sound's best similarity and the source level every second.

## Profiles

`examples/wow-fishing.toml`:

```toml
source = "app:World of Warcraft"   # app:NAME, device:TEXT, default, or file:PATH.wav
target = "World of Warcraft"       # title of the X11 window that receives the keys
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
key, are ignored. Keys are X key names (`9`, `f`, `F1`, `space`, `Return`) of keys pressed
without modifiers: a live run refuses a name that needs Shift or AltGr on the current layout
(`exclam`, `plus`), since only the plain key would be sent.

## Development

```bash
uv run pytest            # fakes and synthetic audio; the GUI test runs offscreen
uv run pytest -m local   # replays a local recording (not in the repository)
uv run ruff check src tests && uv run ruff format --check src tests
```

`spikes/` holds the experiments that led here and is kept as they were
(`uv run --group spikes python spikes/input_test.py` for the one that needs `evdev`).
