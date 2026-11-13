# Testing sound-reactor on Windows

sound-reactor listens to one application's sound and answers a sound with key presses: here,
fishing in World of Warcraft (Forever client): cast, hear the bite, loot, cast again. It works on
Linux, and on one Windows 11 computer (build 26300: 92 bites in 100 casts, keys, focus, display
kept on and the window all working). This test tells whether it works on yours, above all on
Windows 10. It takes about 45 minutes. Steps 1–5 send no keys to the game; only step 6 does.

Copy the output of each step as you go (see step 7 for what to send back).

## Before you start

- Windows 10 version 2004 or later, or Windows 11 (`winver` shows it).
- The game, and a character that can fish, standing **on safe ground** at the water (not on a
  bridge or a ledge).
- In the game: Fishing on action bar key **9**; *Keybindings > Targeting*: Interact With Target
  on **F**; *Gameplay > Controls*: **Enable Interact Key** and **Auto Loot** on; *Sound*: **Sound
  in Background** on, Ambience and Music off or low.

**Safety.** Start the game the normal way, not "as administrator", and use a normal PowerShell
window for everything below: both must run at the same level, or Windows silently drops the
keys (sound-reactor checks this and refuses to start). Keys only go to the game while it is the
window in front; switching away from it (Alt+Tab, a click elsewhere) or *Stop* ends a run at
once, and so does closing the PowerShell window it was started from. Do not type or click
during a run. Automating a game can break its Terms of Use: run it only for this test, while
you watch.

## 1. Install

In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close PowerShell, open a new one, then:

```powershell
uv tool install --no-sources https://github.com/zDju/sound-reactor/archive/refs/heads/main.zip --with-executables-from sound-event-detector
sound-reactor --version
sounddetector --version
```

This installs two commands: `sound-reactor` and `sounddetector` (the sound detector it uses),
downloaded as zip files; Git is not needed. `--no-sources` is needed (without it the install
fails). If the commands are not found, run `uv tool update-shell` and open a new PowerShell.
To update later (when asked to), run the same install command with `--reinstall` added: it
downloads the current version again.

## 2. Does it hear the game?

Start the game and stand at the water. In PowerShell:

```powershell
sounddetector devices
```

Under *Applications with audio open* find the game, for example `Wow.exe  (pid 1234, active)`.
That executable name is used below as `Wow.exe`; use yours. Then:

```powershell
sounddetector monitor -s app:Wow.exe -r C:\Windows\Media\tada.wav --debug
```

(`tada.wav` is only there because `monitor` needs something to look for.) Fish a few casts by
hand. Every second a line shows `level ... dBFS`: it should go up (around −50 to −20) when the
game makes sound and down when it is quiet, also while PowerShell is in front. Ctrl+C stops it.

## 3. Make the "bite" sound

The detector recognises the splash from short clips of it. **A (recommended):** use the four
splash clips of [wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever) (MIT
licence), exactly the ones we use. **B:** record your own.

**A.** In PowerShell:

```powershell
mkdir $HOME\sound-reactor-test -Force; cd $HOME\sound-reactor-test
$base = "https://raw.githubusercontent.com/rsamf/wow-fishing-forever/96661eaa088f16b96c5f9fd1e8c96e81b6b1d93a/src/wowfish/assets/clips"
foreach ($n in "fishbite", "fishingbobber_1", "fishingbobber_2", "fishingbobber_3") {
    curl.exe -fL -o "$n.wav" "$base/$n.wav"
    sounddetector add "$n.wav" --name bite --length 0.8 --label $n
}
```

Each `add` says which part it took, e.g. `taken from 0.190s to 0.990s of ...fishbite.wav`: the
0.8 s starting at the splash (start 0.190, 0.045, 0.020 and 0.035 s for the four files).

**B.** Record 3–4 bites, one per file. Cast by hand and wait until the bobber has landed and is
quiet; then, in PowerShell, start

```powershell
mkdir $HOME\sound-reactor-test -Force; cd $HOME\sound-reactor-test
sounddetector record -d 60 -o splash1.wav -s app:Wow.exe
```

switch back to the game and wait for the splash. Right after it, switch to PowerShell and press
Ctrl+C, **before looting or pressing anything** (the recording is kept): the splash is then
the loudest sound in it. Do the same for `splash2.wav` to `splash4.wav`, then add each one:

```powershell
sounddetector add splash1.wav --name bite --length 0.8
```

`taken from A to B` should point at the splash, near the end of the recording. If it does not,
delete that clip from `%LOCALAPPDATA%\sound-event-detector\bite` and add the file again with
`--start SECONDS` (a moment before the splash). The window (step 5) can also add recordings:
*New sound…* and *Add clips…* in the Sounds box.

**Then, A or B,** set the sound's options and look at it:

```powershell
sounddetector set bite --threshold 0.8 --pitch-tolerance 0.06 --template-length 0.3 --min-level -55
sounddetector events
```

`events` should show `bite: 4 clip(s), threshold 0.8, ..., min level -55 dBFS, ..., pitch ±6%,
first 0.3s of each clip` and the clip files, each 0.8 s long (`add` printed the times). The
minimum level is lower than on Linux (−40): on Windows the splash measured −48 to −37 dBFS.

Check it, still fishing by hand, 5 casts:

```powershell
sounddetector monitor -e bite -s app:Wow.exe --debug
```

Expected: one `DETECTED bite  similarity=0.8...` shortly after each of your splashes, nothing
when casting or in between (another player's splash nearby may also show up, quieter). Ctrl+C.

## 4. The profile, and `sound-reactor doctor`

Get the example profile and put in the game's executable:

```powershell
mkdir "$env:APPDATA\sound-reactor\profiles" -Force
curl.exe -L -o "$env:APPDATA\sound-reactor\profiles\wow-fishing.toml" https://raw.githubusercontent.com/zDju/sound-reactor/main/examples/wow-fishing-windows.toml
notepad "$env:APPDATA\sound-reactor\profiles\wow-fishing.toml"
```

In Notepad, replace `app:REPLACE-ME.exe` with `app:Wow.exe` (your name from step 2) and save.
With the game running and making sound:

```powershell
sound-reactor doctor wow-fishing
```

Expected: the source is *playing audio* and *its windows* include the game's window; the target
window is found (*focused: no* is normal here), with its keyboard layout; keys `9` and `f` with
a scan code; *administrator rights: OK*; `bite` with 4 clips; *start: ready*; *warnings: none*. If the game's
window has another title, use that title as `target =` in the profile. Copy the whole output:
`sound-reactor doctor wow-fishing | Set-Clipboard`.

## 5. Listen only

```powershell
sound-reactor gui wow-fishing
```

Under the buttons it should say *Ready to start.* Click **Listen only** and fish 5 casts by
hand. On each splash the status line shows *Reacting: bite*, and the log `BITE ...` and
`key f (dry run: not sent)`. Nothing is sent to the game. Click **Stop**.

## 6. Start: three casts

Character on safe ground at the water. Click **Start**: the status says *Armed: switch into
World of Warcraft*. Switch into the game (Alt+Tab or a click) and take your hands off the mouse
and keyboard. After 5 s it casts (9); on the bite it presses F, then casts again. It stops by
itself after 3 cycles (a cycle ends with a bite, or after 30 s without one): after 3 bites that
is 4 casts, so the last bobber is still in the water; loot it or let it go by hand. To stop
earlier, switch away from the game or click Stop.

Watch: does 9 cast? Does F loot the fish? Does the character move or anything else unexpected
happen? Does the screen stay on?

## 7. Send back

- The `sound-reactor doctor wow-fishing` output (step 4).
- For the runs of steps 5 and 6: `log.txt` and `counters.json` from each run folder (*Open run
  folder* in the window, or `%LOCALAPPDATA%\sound-reactor\runs\`). Not `audio.wav` files.
- What you saw in step 6, your Windows version (`winver`), and anything that failed in steps 1–3
  (the console output).

If something fails, the message tells what to do:

- `... runs as administrator and sound-reactor does not`: restart the game normally, or run
  PowerShell "as administrator" for sound-reactor too.
- `cannot check whether ... runs as administrator`: the game runs as administrator, under another
  user, or is protected by anti-cheat software. Run sound-reactor from a PowerShell opened with
  "Run as administrator".
- `key '9' needs a modifier on this layout`: on your keyboard layout 9 needs Shift (AZERTY,
  Czech...). Bind Fishing to a key that needs none (e.g. F2) and put that key instead of `9` in
  the profile (`on_start`, the rule and `on_silence`).
- `no window titled '...' is open`: `doctor` shows the game's window titles; put the right one in
  `target =`.
- `nothing called '...' is playing audio now`: the game must be running and making sound; take
  the name from `sounddetector devices`.
- Keys arrive nowhere, with no error: report it, with the doctor output and the run's `log.txt`.
