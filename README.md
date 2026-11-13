# sound-reactor

Hear a sound, press keys: `sound-reactor` listens for sound events with
[sound-event-detector](https://github.com/zDju/sound-event-detector) and answers each with a
list of key presses and waits, sent to one window. Its first use is a test: fishing in World of
Warcraft on Linux (cast, hear the bite, interact, recast). See `PLAN.md` for what was measured
and why.

> **Platform status.** Used on Linux (X11 or XWayland). Windows 10 2004+ / 11 is implemented
> but **unverified on real hardware**; testers follow
> [docs/windows-testing.md](docs/windows-testing.md).

## Safety

- Automating a game can break its Terms of Use. Detection evasion is out of scope: no random
  delays, no memory reading, no injection.
- Keys go to the target window only. A live run only arms at first: it starts when you switch
  into that window, then waits the profile's start delay (at least 5 s).
- It stops when the window loses focus, the audio disconnects, a key or mouse button is held
  before a key would be sent, a limit is reached, or on Ctrl+C or *Stop*
  ([all of it](docs/usage.md#live-runs)).
- On Windows the game and sound-reactor must run at the same privilege level (not one of them
  "as administrator"); this is checked before a start.

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
application's audio. **Windows 10 2004+ / 11**: nothing else.

## Quick start

The sounds are events in the detector library: record them with `sounddetector record` and add
them with `sounddetector add` (see the detector's
[quick start](https://github.com/zDju/sound-event-detector#quick-start)) or with the window's
*New sound…*. Then `sound-reactor gui`:

1. **Profile.** *Profile > New*, or pick one; `examples/wow-fishing.toml` is a complete example.
   Under *Sound and window*, choose what to listen to (`app:NAME` for one application) and the
   window the keys go to.
2. **Sounds.** *Use* adds a library sound, *New sound…* and *Add clips…* cut WAV recordings into
   clips. Threshold and the other settings changed in the table are saved in the profile.
3. **Rules.** *On start*: keys sent first (e.g. `9`). *On sound*: a sound (double-click to
   choose) and its steps, keys such as `f` and `wait SECONDS`. *On silence* and *Stop*: what to
   do when nothing is heard, and the limits. Save with Ctrl+S.
4. **Listen only.** A dry run: everything but the keys, which are logged instead. The meter shows
   each sound's best similarity against its threshold.
5. **Start.** When it says *Ready to start.*, click *Start*, switch into the target window and
   take your hands off the keyboard and mouse. Switch away or click *Stop* to end the run.

The same from the command line:

```bash
sound-reactor doctor wow-fishing          # check everything; paste this when asking
sound-reactor run wow-fishing --dry-run   # everything but the keys; no focus needed
sound-reactor run wow-fishing             # live: switch into the game to start
```

## Documentation

- [Using sound-reactor](docs/usage.md): commands, live and dry runs, how keys are sent, doctor,
  the window, files and folders
- [Profiles](docs/profiles.md): the profile format, cycles and rules, key names
- [Testing on Windows](docs/windows-testing.md): the step-by-step guide for Windows testers
- [Development](docs/development.md): tests, CI, the detector checkout
- `PLAN.md`: what was measured and why
