# sound-reactor — plan

`sound-reactor` (working name; until 2026-11-13 `wow-fishing-controller`) reacts to sound events
with actions. Its first use, and the test of whether the idea works, is fishing in World of
Warcraft Forever on Linux: cast, hear the bite, loot, repeat. After that works well, it becomes
a general project.
Bite detection comes from [sound-event-detector](https://github.com/zDju/sound-event-detector)
(sibling folder `../sound-event-detector`); this project adds the game-specific part: the fishing
cycle, keyboard input, and safety.

> **Risk.** This is an official Blizzard client (`_forever_`, EU, Battle.net). Automating gameplay
> breaks the Terms of Use and can get the account banned. Out of scope, as for the detector:
> memory reading, injection, packet manipulation, and any anti-cheat or detection evasion.

## Facts established (2026-11-13)

**Game and display**
- Steam (non-Steam shortcut) + Proton; prefix `~/.local/share/Steam/steamapps/compatdata/4247171587`,
  game in `.../drive_c/Program Files (x86)/World of Warcraft/_forever_`.
- The game window is an **X11 window under XWayland**: `DISPLAY=:1`, `WM_NAME` "World of Warcraft",
  `WM_CLASS` `steam_app_4247171587` (the Battle.net launcher has the same class). Proton runs
  without its Wayland driver.
- `GxMaximize 1` (maximised window).

**Input options available without root or system packages**
- `/dev/uinput` is writable by the user (Steam's ACL) → a virtual keyboard (`python-evdev`).
- XTEST into XWayland (`python-xlib`) → fake key presses for X11 windows.
- `xdotool` / `ydotool` are not installed (not needed).

**Bite detection** (measured, see the detector's README)
- PipeWire stream "World of Warcraft", captured alone (`app:World of Warcraft`).
- Event `bite` in `~/.local/share/sound-event-detector/bite`: 4 splash clips, pitch ±6 %,
  0.3 s templates, threshold 0.8, min level −35 dBFS. Two sessions: own bites 9/9, no false
  detections; reported ~0.31 s after the splash starts.
- **One splash per cast, at the bite**; casting and the bobber landing make no similar sound.
- Other players' splashes are real matches, ~7 dB quieter; the level gate drops them.
- The game mutes itself when unfocused unless *Sound in Background* is on (it is now).

## Fishing cycle

```text
READY ──press Fishing key──► CASTING ──~1.5 s──► WAITING ──bite heard──► REACTING
  ▲                                                │ no bite in ~30 s        │ press Interact
  │                                                ▼                         ▼
  └──────────────────────── recast ◄──────────── (timeout)              LOOTING (~1–2 s)
```

- A bite counts only while WAITING after our own cast, once per cast. A splash at any other
  time (another player, the previous cast) is ignored.
- Bite → Interact delay: as short as the game allows; measure.
- Stop on: Ctrl+C, a session time limit, the game window losing focus, the game audio stream
  disappearing, or N casts in a row without a bite.

## In-game setup (no addon needed)

From wow-fishing-forever's instructions for this client; that bot loots with the Interact key
and uses vision only to confirm the bobber is targeted:

1. *Options > Gameplay > Controls*: enable **Auto Loot**.
2. *Options > Keybindings > Targeting*: bind **Interact With Target** (e.g. `F`).
   Also check **Enable Interact Key** under Gameplay > Controls if available; the binding alone
   does not enable soft interaction. This feature's behavior on the current Forever client is
   not verified (see Phase 2 below).
3. Put **Fishing** on an action bar key (e.g. `1`).
4. Optional: `/console SoftTargetIconGameObject 1` shows a gold gear icon over the bobber when
   it can be interacted with (only needed if we add a visual check).
5. *Options > Sound*: Ambience and Music to 0, Sound Effects up (detection works with music,
   but quieter background widens the margin); *Sound in Background* on.

## Open questions (to verify in the game)

1. **Does Interact loot the bobber reliably** every time it is pressed after a bite? If not,
   add the visual check (gear icon) or find the bobber on screen, as wow-fishing-forever does.
2. **Which key presses does the game accept**: XTEST, uinput, or synthetic X events
   (`XSendEvent`) sent to the window? Only the last could work while another window has focus;
   the first two require the game to be focused.
3. **Foreground or background?** Is it acceptable that the computer cannot be used while fishing
   (game focused), or should it work in the background?

## Phase 1 results: key input (2026-11-13, `spikes/input_test.py`)

Pressing B (bags) into the game, checked with window grabs:

| Method | Game focused | Game not focused | Notes |
|---|---|---|---|
| **XSendEvent** to the game window | works | ignored | no prompt; can only reach the game window |
| uinput virtual keyboard | works | not tried | goes to whatever window has focus |
| XTEST (XWayland) | works once allowed | not tried | Plasma asks for permission (`kwin_eis_prompter`) |

**Decision: XSendEvent.** The bot runs only while the game has focus and pauses otherwise
(background operation is not possible this way). Focus is reliable to check: XWayland's
`_NET_ACTIVE_WINDOW` is the game window, or KWin's `0x200000` when a Wayland window is focused.
Window grabs (`XGetImage` on the game window) work and show the game.

Keybinds: Fishing `9`, Interact With Target `F`, Auto Loot on.

## Phase 2: partial live tests (2026-11-13)

`spikes/fish_cycle.py` was run twice with a target of five casts. Neither run completed:

- 11:10–11:11, `/tmp/sound-reactor-test-20261113-01`: three casts attempted. First timed out
  after 30 s; second detected a bite at 12.6 s (similarity 0.89, −31.6 dBFS) and sent F.
  Third was interrupted by loss of focus or audio (the original log did not distinguish).
  The user initially was not watching, then reported that F did not work; successful loot has
  not been established. The original summary counted
  only completed cycles and incorrectly reported two casts; this is now fixed.
- 11:12–11:13, `/tmp/sound-reactor-test-20261113-02`: two casts attempted. First timed out;
  second was interrupted by loss of game focus. No bites detected and no Interact sent.
- 11:16–11:18, `/tmp/sound-reactor-test-20261113-03`: all five attempts completed after the
  user enabled Enable Interact Key and confirmed manual casting + F works. Bites detected
  on casts 1, 4 and 5 (21.7 s / 0.95 / −28.5 dBFS; 8.0 s / 0.89 / −26.1 dBFS;
  18.1 s / 0.83 / −33.5 dBFS). F was sent after each. Casts 2 and 3 timed out at 30 s.
  Post-Interact screenshots were saved for all three detections. The first shows an item
  notification. User confirmed all three F presses successfully interacted: one fish caught,
  two "fish got away" outcomes attributed by the user to low fishing skill. Both timeout
  casts had audible, quieter bites: detection was 3/5, with two confirmed misses. Ambience
  was enabled at 30%, and harbor ships were audible during the session.
- 11:26, `/tmp/sound-reactor-test-20261113-04`: requested three casts with gate −40 and debug
  logging. Only the first cast was sent; the user reported unexpected forward movement and
  falling from the bridge into the water after the cast. No bite detected and **no F sent**.
  The script stopped on focus loss about seven seconds after sending 9. The user confirmed
  focusing the game by mouse click. Cause is unknown; this resembles the earlier B-key test
  movement incident, so do not assume uinput or Interact caused either incident. No further
  live input was sent. The −40 gate has not yet had a complete live bite test.

Logs and post-Interact screenshots are outside the repo in the paths above.
The spike now distinguishes focus loss from audio loss and stops on either after starting.
Default startup still requires a focus switch into the game; `--start-focused` allows an
explicitly approved test to start with the game already focused, with a five-second delay.
User feedback after the second test: Enable Interact Key had been disabled. The user enabled
it and confirmed that manually casting and pressing Interact works. This resolves the
initial manual interaction failure on this installation. A
[Forever bug report dated 2026-11-06](https://us.forums.blizzard.com/en/wow/t/interact-with-target-key/2403003)
reports that this binding no longer interacts with fishing bobbers; that report does not
explain this installation's disabled setting. Do not switch to mouse input based on it.
The third live test is finished and stopped. Interaction works; quiet-bite detection needs
improvement. The saved gate is −35 dBFS and similarity threshold 0.8. The quietest detected
bite was −33.5 dBFS, so a level-gate rejection is plausible, but the two missed bites were
not recorded and their actual levels/scores are unknown. Ambience/ships share the same
application stream and can also affect similarity.

Prepared the spike for a controlled next test with `--min-level -40 --debug`, keeping
similarity at 0.8. Overrides apply only to that invocation; the detector library's saved
settings remain unchanged. Debug logs show each second's best similarity *after* the level
gate and the last audio block's level, not the exact level of each rejected match. These
logs alone cannot definitively distinguish every gate rejection from a poor match; recording
and offline scan may still be needed. A relaxed gate can also admit other players' splashes.

Offline check using an existing reference clip, with amplitude scaled according to the
matcher's window-level measurement: a −32 dBFS match is detected at gates −35 and −40;
a −38 dBFS match is rejected at −35 and detected at −40, with similarity ~0.98. This verifies
the gate behavior, not the cause or solution for the live misses. The script compiles and its
CLI help runs without contacting the game. The fourth test above was subsequently started
with the user's explicit approval and aborted due to the movement incident.

Added input diagnostics after that incident: before each cast/Interact the spike reads and
logs X11's held keycodes and mouse buttons, and stops without sending the key if anything is
held. `xwin.send_key` attempts KeyRelease in a `finally` block and synchronizes X11 events.
Offline checks confirm release after an interrupted hold and decoding both held mouse buttons.
These safeguards are not a diagnosis or verified fix: X11 state cannot reveal stale state
inside Wine/the game or rule out input races. Next approved input test should use Alt+Tab,
with hands off mouse/keyboard and the character on safe ground, and be limited to one cast
to investigate movement before resuming detection measurements.

### Runs 05–08 (2026-11-13, 11:30–11:41)

All started by Alt+Tab into the game, hands off, `--min-level -40 --debug`, similarity 0.8.
Game audio was recorded in parallel (`../sound-event-detector/recordings/wow-session-3.wav`,
`wow-session-4.wav`, local only) and scanned afterwards with `sounddetector scan`.

- **05**: focus left before the first cast; nothing sent.
- **06**: one cast; the user left the game after 16 s (moving to another spot). No bite in the
  recording during those 16 s (best match anywhere 0.70, all background at −35…−41 dBFS).
- **07**: 3 casts, bites at 8.6 s (0.85, −28.5 dBFS) and 18.2 s (0.93, −30.1 dBFS), F sent
  for both; third cast no bite in 30 s and no splash-like sound in the recording either
  (all matches in that window < 0.70). Interact, Auto Loot and the auto-loot key were off in
  the game during this run (the user found and enabled them afterwards), so loot not judged.
- **08**: 3 casts, 3 bites (15.7 s / 0.94 / −26.0 dBFS; 25.5 s / 0.93 / −33.9 dBFS;
  7.3 s / 0.96 / −29.1 dBFS), F sent after each. **The user confirmed 3 of 3 caught**;
  chat shows "You receive loot: [Raw Slitherskin Mackerel]" with Fishing skill-ups.
  First complete cycle run: cast → bite → Interact → auto loot, 3/3.

Across 07–08 the recording holds exactly the 5 detected splashes above 0.75 (5/5, no false
detections); background peaks at 0.72 / −38 dBFS, so 0.8 has margin. All bites were
−26…−34 dBFS: the −40 gate was not needed for any of them. No key or mouse button was ever
held before a key was sent, and no unexpected movement was reported in these runs.

### Runs 09–10: large test (2026-11-13, 11:52–12:04)

Target 100 casts, `--min-level -40 --debug --max-misses 5` (new: stop after N empty casts in a
row), audio recorded to `../sound-event-detector/recordings/wow-session-5.wav` (16 min).

- **09**: 12 casts, 12 bites, F after each; stopped on cast 13 when the user clicked outside.
- **10**: 19 casts, 19 bites, F after each; stopped on cast 20 by **KDE's idle screen lock**
  (`kscreenlocker_greet` started 12:04:03, focus lost 12:04:04): the user had left the laptop,
  and XSendEvent keys do not count as user activity. Fix for later runs: run under
  `kde-inhibit --screenSaver` (not yet verified live); v1 inhibits the screen saver itself.
- The user ended the test at 31 casts: "this works".

**31/31 bites answered, no empty casts, no false detections.** Bites came 6.1–25.7 s after the
cast (30 s timeout leaves ~4 s); similarity 0.81–0.96, level −26.3…−34.9 dBFS. Offline scan
of the recording: 33 matches ≥ 0.75; 31 are the live detections, the other two are the bites
of the two interrupted casts (11:56:24, 0.955; 12:04:09, **0.765**, −28.1 dBFS, which 0.8 would
have missed). Background peaks at 0.716 / −37…−38 dBFS. Loot was not checked per cast (the
user watched; loot confirmed in 08).

Conclusions for v1 defaults: similarity 0.8 works (one bite in 33 scored 0.765; 0.75 would
keep ~0.03 margin over background — not changed), gate −40 (one bite at −34.9 is right at the
library's −35), 30 s silence timeout, inhibit the screen lock during live runs.

## Phases

1. **Input spike.** A script presses one harmless key in the game (e.g. opens the bags) via XTEST,
   then uinput, then XSendEvent; note which work focused / unfocused. Nothing else automated.
2. **Interact spike.** Cast by hand; when the detector reports the bite, the script presses
   Interact. Does the fish get looted? Repeat 10×.
3. **Controller MVP.** State machine + detector + chosen input. CLI: `run` (with `--minutes`,
   `--dry-run` that logs instead of pressing keys), keybinds in a TOML config. Logs every cycle.
4. **Measure.** 30+ casts: looted / missed bites / false triggers / recasts. Record the audio of
   the session for re-analysis with `sounddetector scan`.
5. **Repository.** Clean up, README, tests for the state machine (fake detector and fake input),
   then create the GitHub repository.

Phases 3 and 4 run in parallel from 2026-11-13: v1 below is built while the 100-cast test
(phase 4) runs with `spikes/fish_cycle.py`.

## v1: engine, profiles, CLI and GUI (agreed 2026-11-13)

Goal: a general "hear a sound → press keys" tool whose **first version only has to cover the
fishing test**; everything else comes later. Decisions: three rule kinds, keys and waits only
(no mouse), GUI in PySide6.

### Profile (TOML)

Stored in `~/.config/sound-reactor/profiles/<name>.toml`; an example fishing profile goes in
`examples/`. Shape (names may change during implementation, meaning may not):

```toml
source = "app:World of Warcraft"     # detector source: app:NAME, device:TEXT, file:PATH.wav
target = "World of Warcraft"         # X11 window title that receives keys

[sounds.bite]                         # detector event from the library; optional overrides
min_level_db = -40

[on_start]
actions = [{ key = "9" }]

[[on_sound]]
sound = "bite"
actions = [{ wait = 0.25 }, { key = "f" }, { wait = 2.5 }, { key = "9" }]

[on_silence]
seconds = 30
actions = [{ key = "9" }]

[stop]
max_minutes = 60
max_silent_in_row = 5
max_cycles = 100
```

### Cycle semantics (generalised from `spikes/fish_cycle.py`)

- A **cycle** starts when an action list finishes (`on_start`, an `on_sound` rule or
  `on_silence`).
- Sounds that start within `ignore_after_action` (default 1.5 s) after the last key sent are
  ignored, and so are detections queued while actions were running.
- The first matching sound in a cycle runs its rule's actions; nothing else triggers until they
  finish (once per cycle).
- No trigger within `on_silence.seconds` of the cycle start → `on_silence` actions → new cycle.
- Stop on: target losing focus (always), source audio disconnecting, `max_minutes`,
  `max_silent_in_row`, `max_cycles`, a key/mouse button held before sending, Stop/Ctrl+C.

### Safety (unchanged from the spike)

- Start only arms: the run begins on a switch *into* the target window (Alt+Tab), then a
  5 s delay. Focus loss stops it.
- Before each key: check focus and X11 held keys/buttons; KeyRelease in `finally`.
- Keys via XSendEvent to the target window only (X11/XWayland windows; native Wayland apps
  cannot be targeted in v1).
- **Dry run**: everything runs, keys are logged instead of sent; no focus needed.
- No random delays or other "humanising" of input (detection evasion is out of scope).

### Additions

- **Replay a recording**: `source = "file:session.wav"` with dry run shows what the profile
  would have done, without the game. Used for development and checks against
  `../sound-event-detector/recordings/` (local only, never committed).
- **Session recording**: optionally record the source audio of a run for `sounddetector scan`.
- **Run log**: one folder per run (log, counters, optional recording) outside the repository.
- **Live meter**: best similarity and level per second, per sound.

### Structure

- `sound_reactor.profile`: load/save/validate TOML.
- `sound_reactor.engine`: the cycle, Qt-free, own thread; detector, input, focus check and
  clock injected, so it is testable with fakes. Reports log lines, counters and meter values
  through callbacks.
- `sound_reactor.x11`: XSendEvent input, window list/focus, held-input check (from
  `spikes/xwin.py`).
- `sound_reactor.cli`: `sound-reactor run PROFILE [--dry-run] [--record WAV]`.
- `sound_reactor.gui`: PySide6 window over the same engine and profiles:
  profile picker (new/load/save); source app and target window pickers; sounds (detector events
  with clip count, threshold and min level saved to the detector library, "add clip from WAV
  file", live meter); rules editor (on start / on sound / on silence, each a list of key and
  wait steps); stop conditions; Start/Stop, dry run, record, counters, log view.
- Tests: engine with fakes (detector, input, focus, clock), profile round-trip, GUI smoke test
  offscreen (`QT_QPA_PLATFORM=offscreen`); ruff.
- `spikes/` stays as it is, as the record of the experiments.

### Later (not v1)

Record-and-trim clips in the GUI, mouse clicks, uinput input, Windows (see "Windows" below),
a global stop hotkey, several profiles at once, tray icon.

### v1 status / deviations (2026-11-13)

Built: `profile`, `engine`, `x11`, `cli`, `gui` as above, plus `listen` (live listener over
`SoundDetector`, replay listener, recording tee), `runner` (profile → run folder + engine,
shared by CLI and GUI) and `screensaver`. 94 tests (fakes, synthetic audio, GUI offscreen) and
ruff pass. Not exercised: real key sending and the GUI on a real display (deliberately, during
the live test), and the screen-lock inhibition against the real session bus (the screen was
locked; only a unit test with a fake).

Replay of `wow-session-4.wav` with the fishing profile (dry run): all 5 bites trigger
(01:05.6, 01:26.7, 03:18.8, 03:47.2, 03:57.6 by sound start), nothing else, no ignored
sounds; 4:21 of audio in ~11 s. `wow-session-5.wav` (after the review fixes): 32 bites, nothing
else, stopped by `max_silent_in_row` after the session's end; 16 min of audio in ~33 s.

Deviations and choices:
- **Replay runs in audio time.** The library's `file:` source paces at real time through
  `SoundDetector`; v1 instead feeds the file to the library's `Matcher` itself and uses the
  audio position as the engine's clock, so waits and timeouts are seconds of the recording and
  a replay takes seconds. Replays require dry run; their log times are recording positions.
- **Profile:** `ignore_after_action` (1.5) and `start_delay` (5) are top-level settings;
  `[sounds.X]` may be empty; a sound named only by a rule is detected too.
- **Limits:** when a trigger ends the last allowed cycle (`max_cycles`), its whole action list
  runs (the bite is looted) including the trailing recast; when a silent cycle reaches
  `max_cycles` or `max_silent_in_row`, `on_silence` actions are skipped. `max_minutes` counts
  from the first action (not while armed) and can stop mid action list.
- **Dry run** starts at once, without the start delay.
- **CLI extras:** `--source` (replays), `--meter` (instead of the spike's `--debug`),
  `--runs-dir`; `--record` takes an optional WAV (default `audio.wav` in the run folder);
  `sound-reactor profiles`, `sound-reactor gui [PROFILE]`. Run folders:
  `~/.local/state/sound-reactor/runs/`.
- **Live meter:** best (level-gated) similarity per sound plus the source level of the last
  block; a per-sound level of the best window is not exposed by `SoundDetector`.
- **GUI (reworked after the first live use):** profile left, run right (coloured status line,
  Start / Try without keys / Stop, problems under the buttons, counters, meter bars with a
  threshold marker, a summary with "Open run folder", log); New/Open/Save as/Replay in a
  Profile menu; "Advanced" folded; action lists edited as text; threshold and min level edits
  stay in the profile (no "Save to library"); remembers the last profile and the window size;
  asks before dropping unsaved changes. The engine reports its phase through `on_state`.
- **Screen lock (added after runs 09–10):** a live run holds
  `org.freedesktop.ScreenSaver.Inhibit` (session bus, via `jeepney`) from the switch into the
  target until it stops; failure is logged and the run continues (a lock then stops it through
  the focus check).
- **Not ported from the spike:** post-Interact screenshots; the held-input state is logged only
  when something is held (and the run stops).
- **Dependencies:** `pyside6-essentials` (the QtWidgets subset of PySide6), `jeepney`; dev:
  `pytest`, `ruff`; `evdev` only in the `spikes` group. `spikes/` is excluded from ruff.
- **After the review (12:45):** keys needing a modifier on the live layout are refused before a
  run; `--record FILE` never overwrites; live start delay ≥ 5 s; SIGTERM/SIGHUP stop like
  Ctrl+C; a live capture failure is an error with its cause in the run log; sound overrides
  bounded (threshold in (0, 1], min level ≤ 0); the GUI keeps loaded values exactly and Save
  never overwrites a profile opened outside the profiles folder.

## Windows (planned 2026-11-13)

Goal: the same tool on Windows 10 2004+ / 11, tested by the user's friends or on the user's
other computer. Nothing can be verified on Windows from this machine, so every Windows piece is
marked unverified until a tester confirms it.

1. **Detector: per-application capture** (in `../sound-event-detector`, application-neutral).
   `app:NAME` (executable name, e.g. the game's `.exe`) and `app:PID` through WASAPI process
   loopback (`AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`, include the process tree);
   `sounddetector devices` lists applications with audio sessions. Without it only the whole
   output can be captured, so voice chat and music would mix in. Started 2026-11-13 in a git
   worktree of the detector, uncommitted, in parallel with the v1 review fixes.
2. **sound-reactor: Windows backend** next to `x11.py`, behind the engine's existing
   `Keyboard` / focus / `Inhibitor` interfaces:
   - keys: `SendInput` with scan codes (games read scan codes); it goes to the foreground
     window, so the same rule holds: send only while the target window is in the foreground,
     checked right before each key; start on a switch into the target;
   - focus and window list: `GetForegroundWindow`, `EnumWindows` (match by title);
   - held keys/buttons before each key: `GetAsyncKeyState`;
   - screen lock: `SetThreadExecutionState` (injected input probably resets the idle timer
     anyway — unverified);
   - **UIPI:** if the game runs as administrator and sound-reactor does not, Windows drops the
     keys silently; detect and warn;
   - profile key names become platform-neutral (`9`, `f`, `F1`, `space`, …), each backend maps
     them; modifiers are still not sent;
   - config, profiles and run folders in the platform's locations (`platformdirs`);
   - Linux-only dependencies (`python-xlib`, `jeepney`) get `sys_platform` markers; SIGHUP
     handling only where it exists;
   - CI on ubuntu-latest and windows-latest (like the detector).
3. **`sound-reactor doctor`**: prints platform, audio applications, whether the target window is
   found and focused, the key mapping, and warnings (UIPI, missing sounds). Testers send its
   output when something fails.
4. **Tester guide** (`docs/windows-testing.md`), each step safe before the next:
   `sounddetector selftest` → fish by hand with `sounddetector monitor -s app:<game>.exe`
   (no keys) → `sound-reactor doctor` → `sound-reactor run … --dry-run` while fishing by hand →
   live 3 casts → a longer run. Send back the run folders. The splash clips cannot be shared:
   testers get them from the same third-party source or record their own
   (`sounddetector record`, then `sounddetector add`).
5. **Packaging, after testers confirm:** `uv tool install git+<repo>` first (needs the GitHub
   repository); later GitHub Actions builds a PyInstaller bundle per OS on each release
   (Windows zip, Linux AppImage; ~150–250 MB because of Qt, numpy, scipy). No Flatpak/Snap:
   their sandboxes block other applications' audio streams and windows.

Order: 1 (running) → 2–4 (after the user checks v1 in the game) → testers → 5.

## Not decided yet

Language/stack follows the detector (Python 3.12+, uv, pytest, ruff). Vision only if question 1
fails.
