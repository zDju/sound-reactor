# Using sound-reactor

## Commands

```bash
sound-reactor doctor wow-fishing               # check everything; paste this when asking
sound-reactor profiles                         # profiles in the config folder (below)
sound-reactor run wow-fishing --dry-run        # everything but the keys; no focus needed
sound-reactor run wow-fishing                  # live: switch into the game to start
sound-reactor run wow-fishing --record         # also record the source audio
sound-reactor run wow-fishing --dry-run --source file:session.wav   # replay a recording
sound-reactor gui [wow-fishing]                # the window
```

In a clone, run them with `uv run sound-reactor ...`. `--meter` prints each sound's best
similarity and the source level every second.

## Live runs

A **live run** only arms at first: it starts when you switch into the target window
(Alt+Tab), then waits the profile's start delay (at least 5 s). It stops when:

- that window loses focus;
- the audio disconnects;
- a key or mouse button is held before a key would be sent;
- a limit is reached;
- or on Ctrl+C, SIGTERM, SIGHUP (Linux), Ctrl+Break (Windows) or *Stop*.

On Windows, closing the console window it runs from stops it too: Windows ends the process once
told, so the run gets up to 4 s to release its key and write its log and counters.

## How keys are sent

Keys go to the target window only.

- **Linux**: keys are sent with XSendEvent; those do not count as user activity, so a live run
  keeps the screen from locking (`org.freedesktop.ScreenSaver.Inhibit`), since a screen lock
  would take focus.
- **Windows**: keys are sent with SendInput as scan codes, to the target only while it is the
  foreground window. SendInput reaches the foreground window, so focus is checked again right
  before each key and after it. SendInput counts as user activity, and a live run also keeps the
  display on (`SetThreadExecutionState`, which does not stop a screen saver). The game and
  sound-reactor must run at the same privilege level (not one of them "as administrator"), which
  is checked before a start.

## Doctor

`sound-reactor doctor [PROFILE]` prints versions, the applications playing audio and the
profiles; for a profile also:

- whether its source plays;
- whether its window is open and focused;
- what each key is sent as;
- the administrator check (Windows);
- each sound's clips and settings;

then every warning. It sends nothing and writes nothing.

## The window

`sound-reactor gui` has the profile on the left and the run on the right: a coloured status
line, *Start* (live), *Listen only* (dry run) and *Stop*, what would block a start, counters, a
meter per sound with a marker at its threshold, and the log.

- Threshold, min level, pitch tolerance and clip length changed in the sounds table are saved in
  the profile; otherwise the library's values apply.
- *Use* adds a library sound; *New sound…* and *Add clips…* cut WAV recordings into clips.
- A rule's sound is chosen from a list (double-click); the action fields' tooltips list the key
  names.
- It remembers the last profile and its size (`gui.conf` in the config folder).

## Dry runs and replays

A **dry run** logs the keys instead of sending them. A replay (`file:` source, dry run only)
runs in audio time, faster than real time: waits and timeouts are seconds of the recording, and
log times are positions in it.

## Files and folders

| Folder | Linux | Windows |
|---|---|---|
| config | `~/.config/sound-reactor` | `%APPDATA%\sound-reactor` |
| state | `~/.local/state/sound-reactor` | `%LOCALAPPDATA%\sound-reactor` |

The config folder holds `profiles/` and `gui.conf`, the state folder `runs/`. On Linux they
follow `$XDG_CONFIG_HOME` and `$XDG_STATE_HOME`.

Each run writes a folder in `runs/` (state) with `log.txt`, `counters.json`, the profile used
and, with `--record`, `audio.wav` (for `sounddetector scan`; `--record FILE` refuses a file that
already exists).
