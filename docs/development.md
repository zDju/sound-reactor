# Development

```bash
uv run pytest            # fakes and synthetic audio; the GUI test runs offscreen
uv run pytest -m local   # replays a local recording (not in the repository)
uv run ruff check src tests && uv run ruff format --check src tests
```

`uv sync` takes the detector from `../sound-event-detector` (editable), so a clone needs it next
to this folder; CI (`.github/workflows/ci.yml`, Ubuntu and Windows) runs `uv sync --no-sources`
and gets it from GitHub.

The Windows backend (`sound_reactor/win32.py`, ctypes) is tested on every platform against a
simulated Windows (`tests/winfakes.py`); a few tests use the real Windows calls, on Windows only,
and no test ever sends a real key.

`spikes/` holds the experiments that led here and is kept as they were
(`uv run --group spikes python spikes/input_test.py` for the one that needs `evdev`).

See `PLAN.md` for what was measured and why.
