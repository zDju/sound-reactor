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
- The detector is a path dependency (`../sound-event-detector`); a fresh clone needs it next to
  this folder until it becomes a git dependency.
