# sound-reactor

Reacts to sound events with actions, built on the user's own library `../sound-event-detector`
(github.com/zDju/sound-event-detector). Working name; until 2026-11-13 the folder was
`wow-fishing-controller`. The first and current use is a test: fishing in World of Warcraft
Forever on Linux. Once that works well, the user wants a generalised project. Read `PLAN.md` first: it records
what was measured about the game, the display, input options and bite detection, and the phases.

- Keep everything game-specific here; the detector library must stay application-neutral.
- Never send key presses or clicks to the game without the user's go-ahead for that test: the
  user may be typing elsewhere, and input goes to whatever window has focus.
- Game audio and screenshots are copyrighted / personal: keep them out of the repository.
- Repository: github.com/zDju/sound-reactor (branch `main`). Commit as `zdju <zdju@rocketmail.com>`
  (set locally in the repo; the global email is the work address), only with the user's approval,
  and without any Co-Authored-By or other tool trailer.
- The detector dependency is a GitHub archive at a pinned commit
  (`.../sound-event-detector/archive/<commit>.zip`: installs need no Git); bump the commit in
  `pyproject.toml` (and `uv lock`) after a detector commit that sound-reactor needs.
  `[tool.uv.sources]` replaces it with the editable `../sound-event-detector` for development, so
  `uv sync` in a clone needs that folder next to this one. Installs
  (`uv tool install --no-sources https://github.com/zDju/sound-reactor/archive/refs/heads/main.zip
  --with-executables-from sound-event-detector`) and CI (`uv sync --no-sources`) take the pinned
  archive; without `--no-sources` uv looks for the path.
- Windows (`win32.py`) is verified on one Windows 11 computer (2026-11-13, see `PLAN.md`) and
  not yet on Windows 10; testers follow `docs/windows-testing.md`.
  Tests use the simulated Windows in `tests/winfakes.py` and never call the real SendInput.
