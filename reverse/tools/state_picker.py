#!/usr/bin/env python3
"""
Colour + pattern picker for the Rainy 75 board-state indicators
(needs firmware with CONFIG_RGB_MGMT=y).

rainy75_think.py shows six states — orange breathe (working), cyan snake (needs
you), chartreuse blink (YubiKey), crimson flatline (turn died), gold drum
(transient API failure), emerald decay (finished). This is the tool those looks
were chosen with, and the tool to re-choose them in: it picks the COLOUR and the
MOTION separately, because they fail for different reasons.

Geometry, palette and the shipping primitives all come from rainy75_anim, the
same module the daemon renders from — so a colour or tempo tuned here is the
colour and tempo that ship. Only the not-chosen primitives (glyph, bar, sparkle,
bounce) live in this file.

Five modes:

    state_picker.py colors     # candidate hues per state, steady fill  (default)
    state_picker.py versus     # candidate vs the colour it could be confused with,
                               #   BOTH ON THE BOARD AT ONCE, split left/right
    state_picker.py patterns   # the motion primitives, in neutral white
    state_picker.py states     # the six looks, entrance + sustain, as they ship
    state_picker.py all        # colors, versus, patterns, states

Note the ids you like (err-2, stl-4, pat-5) and report them.

Why `versus` exists: shown one after the other your eye adapts between two
colours and both look fine — that is how you ship an amber nobody can tell from
the orange. Shown side by side on the same board they either separate or they
don't. Both shipping picks ended up FURTHER from their incumbent than first
proposed, which is the no-gamma-correction tax; judge there, not on screen.

Why `patterns` is neutral white: motion and hue are independent choices, and a
pattern you dislike in red you may like in gold. Pick the movement first.

Like green_picker.py this drives the Rainy75 client in-process rather than
shelling out per frame — a subprocess per frame cannot animate evenly and
re-opens the serial port every time.

Two geometry caveats, both worth seeing rather than reading about:

  * The bottom row is 10 keys spanning the full width (Space alone is ~6 units),
    so `col` there is nothing like physical x. Anything that sweeps by column —
    `wipe`, `burst` — will look ragged along that row.
  * On ANSI boards two keymap positions have no LED of their own and park on a
    neighbour's (the ISO `<>` slot, and the key right of Space). Full-board
    primitives write to them, so those neighbours get written twice per frame.

Neither affects the chosen sustains: flatline is home-row only, drum is
alpha-only.

Port contention: a running rainy75-think worker holds the port exclusively
(TIOCEXCL), so this waits for it. If it can't get in, run `rainy75-think reset`
in another terminal first. Ctrl-C always restores the board.
"""
import argparse
import importlib.util
import os
import random
import sys
import time

TOOLDIR = os.path.dirname(os.path.abspath(__file__))
if TOOLDIR not in sys.path:
    sys.path.insert(0, TOOLDIR)
import rainy75_anim as anim             # noqa: E402  (needs the path above)

from rainy75_anim import (              # noqa: E402
    ALPHA_SPANS, BOARD_CENTRE, Canvas, DONE_SECS, DRUM_CYCLE_SECS, MAXCOL,
    NROWS, PALETTE, ROW_LENS, dim, pos,
)

# --------------------------------------------------------------------------
# Glyphs, as (row, col) strokes. Drawn key-by-key in list order.
#
# NOT CHOSEN — the shipping looks use flash/wipe/burst instead, which read faster
# and don't depend on the staggered geometry surviving a keycap change. Kept
# because the shapes do work and may be worth revisiting: the ✗ crosses on T and
# includes the X key; the ✓ runs W D C G Y 7 F8.
# --------------------------------------------------------------------------

CHECK = [(2, 2), (3, 3), (4, 4), (3, 5), (2, 6), (1, 7), (0, 8)]
CROSS_A = [(0, 3), (1, 4), (2, 5), (3, 6), (4, 7)]   # F3 4 T H N
CROSS_B = [(0, 7), (1, 6), (2, 5), (3, 4), (4, 3)]   # F7 6 T F X

NUMROW_FIRST = 16     # keys 1..0 — the (not chosen) bar's track
NUMROW_LEN = 10

# --------------------------------------------------------------------------
# Colour candidates
#
# Each state names the incumbent it risks being confused with, so `versus` knows
# what to pair it against. Shipping picks are marked and live in anim.PALETTE.
# --------------------------------------------------------------------------

EXISTING = {
    "think":     ("ff3c00", "orange     — working (breathe)"),
    "attention": ("00e0ff", "cyan       — needs you (snake)"),
    "touch":     ("6bff00", "chartreuse — YubiKey (blink)"),
}

PALETTES = {
    # error: nothing to confuse it with, so this is purely "which red reads as
    # 'stopped' without blooming". Full red across 83 LEDs is very loud.
    "error": {
        "versus": None,
        "hues": [
            ("ff0000", "pure red (harshest, blooms)"),
            ("e00000", "red, slightly held back"),
            ("c80000", "deep red"),
            ("b40000", "dark blood red"),
            ("ff1500", "red, faint orange lean"),
            ("e00020", "crimson (magenta lean)"),
            ("ff0032", "hot crimson              <- SHIPPING"),
            ("8c0000", "very dark red (dimmest)"),
        ],
    },
    # stalled: THE risky one. Must not read as the orange think-breathe.
    # Walking the green channel up moves it away from orange toward gold.
    "stalled": {
        "versus": "think",
        "hues": [
            ("ff8c00", "dark orange (closest to think — too close)"),
            ("ffa000", "amber"),
            ("ffaa00", "amber (a touch yellower)"),
            ("ffb400", "amber-gold"),
            ("ffc800", "gold                     <- SHIPPING"),
            ("ffd200", "yellow-gold (max separation from think)"),
            ("e6a000", "amber, dimmed"),
        ],
    },
    # done: must not read as the chartreuse YubiKey blink. Dropping red and
    # adding blue swings it from yellow-green toward emerald.
    "done": {
        "versus": "touch",
        "hues": [
            ("00ff00", "pure green (closest to chartreuse)"),
            ("00c828", "true green"),
            ("00e04b", "green, slight spring lean"),
            ("00ff50", "spring green"),
            ("00b43c", "forest emerald (dimmest) <- SHIPPING"),
            ("00ff8c", "mint"),
            ("32c850", "sage (desaturated)"),
        ],
    },
}


def _hex(rgb):
    return "%02x%02x%02x" % rgb


# What ships today, straight from the daemon's palette — never a second copy.
CHOSEN = {k: _hex(PALETTE[k]) for k in ("error", "stalled", "done")}

HUE_SECS = 3.5
GAP_SECS = 1.2
VERSUS_SECS = 5.0
PATTERN_SECS = 6.0

ACQUIRE_TIMEOUT_S = 20

WHITE = (0xC0, 0xC0, 0xC0)   # neutral for `patterns` — full white is glaring


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

_rgb_module = None


def _load_rgb():
    global _rgb_module
    if _rgb_module is None:
        spec = importlib.util.spec_from_file_location(
            "rainy75_rgb", os.path.join(TOOLDIR, "rainy75_rgb.py"))
        _rgb_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_rgb_module)
    return _rgb_module


def _load_client():
    return _load_rgb().Rainy75


def _find_port():
    # By USB product name (rainy75_rgb.find_port), not the first serial node,
    # which can be another device such as a monitor's control interface.
    return _load_rgb().find_port()


def _acquire(port):
    """Wait out a rainy75-think worker that still holds the port."""
    client = _load_client()
    deadline = time.time() + ACQUIRE_TIMEOUT_S
    warned = False
    while True:
        try:
            return client(port)
        except OSError as exc:
            if time.time() >= deadline:
                raise
            if not warned:
                print(f"  port busy ({exc.strerror or exc}) — waiting for the "
                      "rainy75-think worker to let go...", flush=True)
                warned = True
            time.sleep(0.5)


def _rgb(hex_):
    hex_ = hex_.lstrip("#")
    return tuple(int(hex_[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# not-chosen primitives (kept for revisiting; the shipping set is in rainy75_anim)
# --------------------------------------------------------------------------

def p_glyph(cv, color, strokes, dt=0.045, hold=0.6, fade=True):
    """Draw each stroke key-by-key, hold the finished glyph, optionally fade."""
    frame = {}
    for stroke in strokes:
        for (r, c) in stroke:
            p = pos(r, c)
            if p is None:
                continue
            frame[p] = color
            cv.show(frame)
            cv.sleep(dt)
    cv.sleep(hold)
    if fade:
        for f in (0.75, 0.5, 0.3, 0.15, 0.0):
            cv.show({p: dim(color, f) for p in frame})
            cv.sleep(0.07)
        cv.black()


def p_bar(cv, color, secs, period=2.4, dt=0.05):
    """Number row as a progress meter, filling and resetting."""
    end = time.time() + secs
    while time.time() < end:
        t0 = time.time()
        while time.time() - t0 < period:
            lit = int(NUMROW_LEN * ((time.time() - t0) / period))
            cv.show({NUMROW_FIRST + s: (color if s < lit else dim(color, 0.06))
                     for s in range(NUMROW_LEN)})
            cv.sleep(dt)
            if time.time() >= end:
                break
    cv.black()


def p_sparkle(cv, color, secs, born=4, dt=0.06, life=8):
    """Random keys twinkle and decay."""
    live = {}
    end = time.time() + secs
    while time.time() < end:
        for _ in range(born):
            r = random.randrange(NROWS)
            live[pos(r, random.randrange(ROW_LENS[r]))] = life
        frame = {}
        for p, age in list(live.items()):
            if age <= 0:
                del live[p]
                continue
            frame[p] = dim(color, age / life)
            live[p] = age - 1
        cv.show(frame)
        cv.sleep(dt)
    cv.black()


def p_bounce(cv, color, secs, dt=0.045):
    """A ball with gravity, squashing on the bottom row."""
    x, y, vy, vx, g = 3.0, 0.0, 0.0, 0.55, 0.16
    end = time.time() + secs
    while time.time() < end:
        vy += g
        y += vy
        x += vx
        if y >= NROWS - 1:
            y, vy = NROWS - 1, -vy * 0.82
        if x <= 0 or x >= MAXCOL - 1:
            vx = -vx
            x = max(0.0, min(MAXCOL - 1.0, x))
        r, c = int(round(y)), int(round(x))
        frame = {}
        for dr, f in ((0, 1.0), (-1, 0.35)):
            p = pos(r + dr, c)
            if p is not None:
                frame[p] = dim(color, f)
        cv.show(frame)
        cv.sleep(dt)
    cv.black()


# --- the three older shipping looks, reimplemented here for comparison only ---

def p_breathe(cv, color, secs, period=1.8, steps=16, min_f=0.05):
    ramp = list(range(steps + 1)) + list(range(steps - 1, -1, -1))
    dt = (period / 2) / steps
    end = time.time() + secs
    while time.time() < end:
        for i in ramp:
            cv.fill(dim(color, min_f + (1.0 - min_f) * (i / steps)))
            cv.sleep(dt)
            if time.time() >= end:
                break
    cv.fill((0, 0, 0))


def p_snake(cv, color, secs, length=6, dt=0.05):
    path = anim.snake_path()
    head = 0
    end = time.time() + secs
    while time.time() < end:
        cv.show({path[(head - k) % len(path)]: dim(color, (length - k) / length)
                 for k in range(length)})
        cv.sleep(dt)
        head = (head + 1) % len(path)
    cv.black()


def p_blink(cv, color, secs, dt=0.5):
    end = time.time() + secs
    on = True
    while time.time() < end:
        cv.fill(color if on else (0, 0, 0))
        cv.sleep(dt)
        on = not on
    cv.fill((0, 0, 0))


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def mode_colors(kb, only=None):
    print(f"\n=== COLOURS: steady fills, {HUE_SECS:g}s each, "
          f"{GAP_SECS:g}s black between ===")
    print("    (steady on purpose — judge the hue with no motion in the way)\n")
    for state, spec in PALETTES.items():
        if only and state != only:
            continue
        risk = spec["versus"]
        note = f"  [confusion risk: {EXISTING[risk][1]}]" if risk else ""
        print(f"  -- {state}{note}")
        for i, (hex_, name) in enumerate(spec["hues"], 1):
            print(f"     {state[:3]}-{i:<2} #{hex_}   {name}", flush=True)
            kb.fill(_rgb(hex_))
            time.sleep(HUE_SECS)
            kb.fill((0, 0, 0))
            time.sleep(GAP_SECS)
        print()


def mode_versus(kb, only=None):
    print(f"\n=== VERSUS: candidate and incumbent ON THE BOARD AT ONCE, "
          f"{VERSUS_SECS:g}s each ===")
    print("    left half = the colour already in use, right half = the candidate,")
    print("    centre column dark. If you have to think about which is which,")
    print("    they're too close.\n")
    cv = Canvas(kb)
    split = MAXCOL // 2
    for state, spec in PALETTES.items():
        if only and state != only:
            continue
        risk = spec["versus"]
        if not risk:
            print(f"  -- {state}: no incumbent to confuse it with, skipping\n")
            continue
        inc_hex, inc_name = EXISTING[risk]
        inc = _rgb(inc_hex)
        print(f"  -- {state}   LEFT = #{inc_hex} ({risk}, {inc_name})")
        for i, (hex_, name) in enumerate(spec["hues"], 1):
            print(f"     {state[:3]}-{i:<2} RIGHT #{hex_}   {name}", flush=True)
            frame = {}
            for r in range(NROWS):
                for c in range(ROW_LENS[r]):
                    if c < split:
                        frame[pos(r, c)] = inc
                    elif c > split:
                        frame[pos(r, c)] = _rgb(hex_)
            cv.show(frame)
            time.sleep(VERSUS_SECS)
            cv.black()
            time.sleep(0.5)
        print()


def mode_patterns(kb, color=None):
    c = _rgb(color) if color else WHITE
    print(f"\n=== PATTERNS: motion only, in neutral #{_hex(c)} ===")
    print("    (hue is a separate choice — pick the movement first)\n")
    cv = Canvas(kb)
    demos = [
        ("flash     (hard whole-board double flash)          [error entrance]",
         lambda: anim.flash(cv, c)),
        ("flatline  (home row held dim, dot + blip crossing) [error sustain]",
         lambda: anim.flatline(cv, c, PATTERN_SECS + 4, period=2.5)),
        ("wipe      (column sweep with a fading trail)       [stalled entrance]",
         lambda: anim.wipe(cv, c)),
        ("drum      (letter keys tapping, hand travels)      [stalled sustain]",
         lambda: anim.drum(cv, c, DRUM_CYCLE_SECS + 4)),
        (f"burst x{anim.DONE_BURSTS}  (expanding rings from one key)      "
         "[done entrance]",
         lambda: anim.burst(cv, c, times=anim.DONE_BURSTS)),
        ("glow-decay(settle, then the fade IS the timer — 12s)[done sustain]",
         lambda: anim.glow_decay(cv, c, 12.0)),
        ("--- not chosen, kept for revisiting ---", None),
        ("glyph  ✓  (check, drawn stroke-by-stroke)",
         lambda: p_glyph(cv, c, [CHECK])),
        ("glyph  ✗  (cross, two strokes crossing on T)",
         lambda: p_glyph(cv, c, [CROSS_A, CROSS_B])),
        ("bar       (number row as a progress meter)",
         lambda: p_bar(cv, c, PATTERN_SECS)),
        ("sparkle   (random keys twinkle and decay)",
         lambda: p_sparkle(cv, c, PATTERN_SECS)),
        ("bounce    (ball with gravity, squashes on the bottom row)",
         lambda: p_bounce(cv, c, PATTERN_SECS)),
    ]
    i = 0
    for name, fn in demos:
        if fn is None:
            print(f"\n  {name}\n", flush=True)
            continue
        i += 1
        print(f"  pat-{i:<2} {name}", flush=True)
        fn()
        time.sleep(0.8)
    print()


def mode_states(kb, colors=None, done_secs=None):
    """The six shipping looks, entrance + sustain, in priority order."""
    colors = colors or {}
    done_secs = DONE_SECS if done_secs is None else done_secs
    err = _rgb(colors.get("error", CHOSEN["error"]))
    stl = _rgb(colors.get("stalled", CHOSEN["stalled"]))
    dne = _rgb(colors.get("done", CHOSEN["done"]))
    print("\n=== STATES: the whole vocabulary, as it ships ===")
    print("    Each persistent state = a short ENTRANCE then its quiet SUSTAIN.")
    print("    Watch whether the six read as one system, and whether you could")
    print("    sit beside the sustains all afternoon.\n")
    cv = Canvas(kb)

    print("  st-1  think      orange breathe            [EXISTING]", flush=True)
    p_breathe(cv, _rgb(EXISTING["think"][0]), 8)
    time.sleep(0.8)

    print("  st-2  attention  cyan comet                [EXISTING]", flush=True)
    p_snake(cv, _rgb(EXISTING["attention"][0]), 8)
    time.sleep(0.8)

    print("  st-3  touch      chartreuse 1 Hz blink     [EXISTING]", flush=True)
    p_blink(cv, _rgb(EXISTING["touch"][0]), 5)
    time.sleep(0.8)

    print("  st-4  error      double flash -> flatline sustain      [NEW]",
          flush=True)
    anim.flash(cv, err)
    anim.flatline(cv, err, 14, period=5.0)
    time.sleep(0.8)

    print("  st-5  stalled    wipe -> travelling drum sustain       [NEW]",
          flush=True)
    print(f"        (one full pass over the letters takes ~{DRUM_CYCLE_SECS:.0f}s)",
          flush=True)
    anim.wipe(cv, stl)
    anim.drum(cv, stl, DRUM_CYCLE_SECS + 4)
    time.sleep(0.8)

    print(f"  st-6  done       {anim.DONE_BURSTS} bursts -> settle -> "
          f"{done_secs:g}s fade   [NEW]", flush=True)
    print("        (the fade is the timer — bright = just finished)", flush=True)
    anim.burst(cv, dne, center=BOARD_CENTRE, times=anim.DONE_BURSTS)
    anim.glow_decay(cv, dne, done_secs)
    time.sleep(0.8)
    print()


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Pick colours and motions for the Rainy 75 board states")
    ap.add_argument("mode", nargs="?", default="colors",
                    choices=("colors", "versus", "patterns", "states", "all"))
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--state", choices=tuple(PALETTES),
                    help="colors/versus: limit to one state")
    ap.add_argument("--color", metavar="RRGGBB",
                    help="patterns: draw in this colour instead of neutral white")
    ap.add_argument("--error", metavar="RRGGBB", help="states: error colour")
    ap.add_argument("--stalled", metavar="RRGGBB", help="states: stalled colour")
    ap.add_argument("--done", metavar="RRGGBB", help="states: done colour")
    ap.add_argument("--done-secs", type=float, default=DONE_SECS, metavar="S",
                    help=f"states: how long `done` decays for (default {DONE_SECS:g})")
    args = ap.parse_args()

    port = args.port or _find_port()
    if not port:
        print("Rainy 75 not found on USB. Plug it in, or pass --port.")
        sys.exit(1)
    print(f"Port: {port}")

    try:
        kb = _acquire(port)
    except OSError as exc:
        print(f"Could not open {port}: {exc}\n"
              "A rainy75-think worker may be holding it — try: rainy75-think reset")
        sys.exit(1)

    chosen = {k: v for k, v in
              (("error", args.error), ("stalled", args.stalled), ("done", args.done))
              if v}

    try:
        if args.mode in ("colors", "all"):
            mode_colors(kb, args.state)
        if args.mode in ("versus", "all"):
            mode_versus(kb, args.state)
        if args.mode in ("patterns", "all"):
            mode_patterns(kb, args.color)
        if args.mode in ("states", "all"):
            mode_states(kb, chosen, args.done_secs)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        try:
            kb.clear()        # back to the normal effect (heatmap)
        except Exception:
            pass
        try:
            kb.close()
        except Exception:
            pass

    print("\nDone — board back to your normal effect.")
    print("Shipping now: " + "  ".join(f"{k}=#{v}" for k, v in CHOSEN.items()))
    print("Override any of them to compare:")
    print("  state_picker.py states --error ff0032 --stalled ffc800 --done 00b43c")


if __name__ == "__main__":
    main()
