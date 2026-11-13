"""Phase 2 spike: full fishing cycles. Cast (Fishing key), wait for the bite, press Interact.

Keys go to the game window only (XSendEvent) and only while it has focus; the script stops
if focus or game audio is lost after starting. Stop with Ctrl+C.

    uv run --with pillow python spikes/fish_cycle.py OUTDIR [CASTS]
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from datetime import datetime
from pathlib import Path

from sound_event_detector import Detection, Library, SoundDetector

sys.path.insert(0, str(Path(__file__).parent))
import xwin  # noqa: E402

CAST_KEY, INTERACT_KEY = "9", "f"
IGNORE_AFTER_CAST = 1.5  # s: the cast itself, splashes of the previous cast
BITE_TIMEOUT = 30.0  # s without a bite -> recast
REACTION = 0.25  # s between hearing the bite and pressing Interact
LOOT_WAIT = 2.5  # s for auto loot before the next cast

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("outdir", type=Path)
parser.add_argument("casts", nargs="?", type=int, default=5)
parser.add_argument("--min-level", type=float, help="Override the minimum bite level in dBFS for this run.")
parser.add_argument("--threshold", type=float, help="Override the similarity threshold for this run.")
parser.add_argument("--debug", action="store_true", help="Log scored similarity peaks and sampled audio levels each second.")
parser.add_argument(
    "--max-misses", type=int, default=5,
    help="Stop after this many casts in a row without a bite (default 5; 0 = never).",
)
parser.add_argument(
    "--start-focused", action="store_true",
    help="Allow starting with the game already focused (only for an explicitly approved test).",
)
args = parser.parse_args()
if args.casts < 1:
    parser.error("casts must be positive")
out = args.outdir
out.mkdir(parents=True, exist_ok=True)
casts = args.casts


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with (out / "log.txt").open("a") as f:
        f.write(line + "\n")


d = xwin.open_display()
win = xwin.find_window(d)
if win is None:
    sys.exit("game window not found")


def focused() -> bool:
    return xwin.active_window_id(d) == win.id


def check_ready() -> None:
    if not focused():
        log("game lost focus; stopping")
        raise KeyboardInterrupt
    if not detector.connected.is_set():
        log("game audio disconnected; stopping")
        raise KeyboardInterrupt


def press_key(key: str) -> None:
    check_ready()
    state = xwin.input_state(d)
    log(f"input before {key!r}: {state}")
    if state["keycodes"] or state["buttons"]:
        log("a key or mouse button is held; stopping without sending this key")
        raise KeyboardInterrupt
    check_ready()
    xwin.send_key(d, win, key)


bites: queue.Queue[Detection] = queue.Queue()
event = Library().load("bite", min_level_db=args.min_level, threshold=args.threshold)
detector = SoundDetector(event, source="app:World of Warcraft")
detector.on_detect(bites.put)
detector.start()
if not detector.connected.wait(10):
    sys.exit("could not capture the game's audio")
log(f"settings: similarity >= {event.threshold:.2f}, minimum level {event.min_level_db:.1f} dBFS")
log(f"listening; {casts} casts. Waiting for game focus.")
# Start only on a switch *into* the game (the user is elsewhere reading instructions first),
# so nothing is cast while the user is still walking around in the game.
seen_other = args.start_focused or not focused()
while not (seen_other and focused()):
    if not focused():
        seen_other = True
    time.sleep(0.1)
log("game focused; first cast in 5 s")
time.sleep(5.0)

results = []
attempted = 0
try:
    for n in range(1, casts + 1):
        check_ready()
        press_key(CAST_KEY)
        attempted += 1
        t_cast = time.time()
        log(f"cast {n}")
        detector.take_peaks()
        next_debug = time.monotonic() + 1.0
        bite = None
        while time.time() - t_cast < BITE_TIMEOUT:
            check_ready()
            if args.debug and time.monotonic() >= next_debug:
                stats = detector.stats()
                peak = detector.take_peaks()[event.name]
                score = f"{peak:.3f}" if peak > -1 else "none (too quiet or no window)"
                log(
                    f"  debug: best gated similarity={score}; "
                    f"last audio block={stats.level_db:.1f} dBFS; "
                    f"analysed={stats.audio_seconds:.1f}s; load={stats.load:.1%}"
                )
                next_debug = time.monotonic() + 1.0
            try:
                det = bites.get(timeout=0.2)
            except queue.Empty:
                continue
            assert det.timestamp is not None
            after = det.timestamp.timestamp() - t_cast
            if after < IGNORE_AFTER_CAST:
                log(f"  ignored a splash {after:.2f}s after the cast")
                continue
            bite = det
            break
        if bite is None:
            log("  no bite in 30 s; recasting")
            results.append((n, None))
            misses = 0
            for _, r in reversed(results):
                if r is not None:
                    break
                misses += 1
            if args.max_misses and misses >= args.max_misses:
                log(f"{misses} casts in a row without a bite; stopping")
                break
            continue
        after = bite.timestamp.timestamp() - t_cast  # type: ignore[union-attr]
        log(
            f"  BITE {after:.1f}s after the cast: similarity {bite.similarity:.2f}, "
            f"level {bite.level_db:.1f} dBFS ({bite.template})"
        )
        time.sleep(REACTION)
        check_ready()
        press_key(INTERACT_KEY)
        log("  pressed Interact")
        time.sleep(1.0)
        xwin.save_png(xwin.grab(win), str(out / f"cast{n}-after-interact.png"))
        results.append((n, after))
        time.sleep(LOOT_WAIT - 1.0)
except KeyboardInterrupt:
    log("stopped")
finally:
    detector.stop()
    got = sum(1 for _, r in results if isinstance(r, float))
    log(f"done: {attempted} casts attempted, {len(results)} completed, {got} bites answered with Interact")
