#!/usr/bin/env python3
"""
Board geometry, palette and animation primitives for the Rainy 75 indicators.

Shared by rainy75_think.py (the daemon that drives the board from Claude Code
hooks) and state_picker.py (the tool used to choose these looks on hardware).
One copy so the two can never drift: a colour or a tempo tuned in the picker is
the colour and tempo that ship.

Every primitive drives a Canvas rather than the keyboard client directly. The
Canvas owns the three things a long-running animation has to get right:

  * abort — SIGTERM from a re-arbitrating _drive(), or the worker's own deadline,
    must land *inside* a 5 s flatline gap, not after it. cv.sleep() slices.
  * dropped frames — a host suspend/resume or a busy port stalls a request. One
    blip is not a dead link; only a run of them is (MAX_CONSECUTIVE_MISSES).
  * sparse output — set_positions() is incremental on the device, so a key stays
    lit until explicitly blacked. Canvas sends only the delta, which is what
    keeps these to a handful of quads per frame.

Nothing here imports the keyboard client or ZMK; a Canvas just needs an object
with .fill((r,g,b)) and .set_positions({pos: (r,g,b)}).
"""
import time

# --------------------------------------------------------------------------
# Board geometry — keymap positions, row-major (matches rainy75_rgb.py's _ROWS)
# --------------------------------------------------------------------------

ROW_LENS = (15, 15, 14, 15, 14, 10)
ROW_START = (0, 15, 30, 44, 59, 73)
NROWS = len(ROW_LENS)
MAXCOL = max(ROW_LENS)
NKEYS = sum(ROW_LENS)

HOME_ROW = 3          # CAPS..PGDN — the flatline's track

# The 26 alpha keys as (row, first_col, last_col) spans — the drum's territory.
# Q..P, A..L, Z..M. Z starts at col 2, not 1: col 1 on that row is the ISO <>
# slot, which on ANSI has no key of its own.
ALPHA_SPANS = ((2, 1, 10), (3, 1, 9), (4, 2, 8))

# Burst origin for `done`: home row, index-finger position (J) — near enough to
# the middle that the ring expands evenly in every direction.
BOARD_CENTRE = (3, 7)


def pos(r, c):
    """(row, col) -> keymap position, or None if that cell has no key.

    Rows are unequal (15/15/14/15/14/10), so every caller has to be prepared for
    a cell that simply isn't there rather than assuming a rectangle.
    """
    if not (0 <= r < NROWS) or not (0 <= c < ROW_LENS[r]):
        return None
    return ROW_START[r] + c


def snake_path():
    """Boustrophedon order over all keys — the `attention` comet's track."""
    path, p0 = [], 0
    for i, n in enumerate(ROW_LENS):
        seg = list(range(p0, p0 + n))
        if i % 2:
            seg.reverse()
        path.extend(seg)
        p0 += n
    return path


# --------------------------------------------------------------------------
# Palette — picked on real hardware with state_picker.py
#
# Both new warm/green values landed FURTHER from their incumbent than first
# proposed (gold past amber, forest emerald past true green). That is the
# no-gamma-correction tax: across 83 saturated LEDs colours read closer together
# than they do on paper, so a paper-safe margin is not enough. Keep the habit.
# --------------------------------------------------------------------------

PALETTE = {
    "think":     (0xFF, 0x3C, 0x00),   # orange     — working
    "attention": (0x00, 0xE0, 0xFF),   # cyan       — needs you
    "touch":     (0x6B, 0xFF, 0x00),   # chartreuse — YubiKey blocked
    "error":     (0xFF, 0x00, 0x32),   # hot crimson— turn died, go look
    "stalled":   (0xFF, 0xC8, 0x00),   # gold       — transient, just wait
    "done":      (0x00, 0xB4, 0x3C),   # forest emerald — finished
}

# `done` holds the board for a minute. Long enough to answer "did that finish
# while I was away?", and the decay carries the extra information for free:
# brightness IS elapsed time. It also retires itself, so MODE_TTL["done"] and the
# animation length are the same number by construction.
DONE_SECS = 60.0

# The drumming hand: keys spanned, tap interval, full taps dwelt before moving.
DRUM_SPAN = 4
DRUM_DT = 0.11
DRUM_CYCLES = 2

MAX_CONSECUTIVE_MISSES = 5
MISS_BACKOFF_S = 0.2
SLEEP_SLICE_S = 0.1


def dim(color, f):
    return tuple(max(0, min(255, int(c * f))) for c in color)


# --------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------

class Aborted(Exception):
    """Raised inside a primitive when the worker should stop rendering."""


class Canvas:
    def __init__(self, kb, stop=None, deadline=None):
        self.kb = kb
        self.stop = stop                  # {"v": bool}, as rainy75_think uses
        self.deadline = deadline
        self.prev = {}
        self.misses = 0
        self.fill((0, 0, 0))              # enter host mode on a known-black board

    # --- abort plumbing ---

    def _check(self):
        if self.stop is not None and self.stop.get("v"):
            raise Aborted
        if self.deadline is not None and time.time() >= self.deadline:
            raise Aborted

    def sleep(self, secs):
        """Sleep in slices so an abort lands promptly — a 5 s flatline gap must
        not outlive a SIGTERM by 5 s."""
        end = time.time() + secs
        while True:
            self._check()
            rem = end - time.time()
            if rem <= 0:
                return
            time.sleep(min(SLEEP_SLICE_S, rem))

    def _emit(self, fn):
        """Run one device write, tolerating blips but not a dead link."""
        self._check()
        try:
            fn()
            self.misses = 0
        except Aborted:
            raise
        except Exception:
            self.misses += 1
            if self.misses >= MAX_CONSECUTIVE_MISSES:
                raise Aborted
            self.sleep(MISS_BACKOFF_S)

    # --- output ---

    def show(self, frame):
        """Paint exactly `frame`; anything lit last time and absent now goes black."""
        out = dict(frame)
        for p in self.prev:
            if p not in frame:
                out[p] = (0, 0, 0)
        if out:
            self._emit(lambda: self.kb.set_positions(out))
        self.prev = dict(frame)

    def fill(self, color):
        self._emit(lambda: self.kb.fill(color))
        self.prev = {}

    def black(self):
        self.show({})


# --------------------------------------------------------------------------
# Primitives — the shipping set
# --------------------------------------------------------------------------

def flash(cv, color, times=2, on=0.12, off=0.12):
    """Hard whole-board flashes. `error`'s entrance — the least ambiguous way to
    say 'this just changed, and not for the better'."""
    for _ in range(times):
        cv.fill(color)
        cv.sleep(on)
        cv.fill((0, 0, 0))
        cv.sleep(off)


def flatline(cv, color, secs, period=5.0, dt=0.03):
    """ECG: the home row held dim, a bright dot crossing it, one blip mid-sweep.

    The blip is what makes it read as a heart monitor rather than a scanner —
    and a heart monitor is the least ambiguous way a keyboard can say 'dead'.
    One sweep per five seconds is visible without nagging, which matters because
    this state can sit for a quarter of an hour.
    """
    n = ROW_LENS[HOME_ROW]
    base = {pos(HOME_ROW, c): dim(color, 0.10) for c in range(n)}
    blip_col = n // 2
    end = time.time() + secs
    while time.time() < end:
        for c in range(n):
            frame = dict(base)
            if c == blip_col:
                up = pos(HOME_ROW - 1, c)
                if up is not None:
                    frame[up] = color
            frame[pos(HOME_ROW, c)] = color
            nxt = pos(HOME_ROW, c + 1)
            if nxt is not None:
                frame[nxt] = dim(color, 0.45)
            cv.show(frame)
            cv.sleep(dt)
            if time.time() >= end:
                break
        cv.show(base)
        rest = min(period - n * dt, end - time.time())
        if rest > 0:
            cv.sleep(rest)
    cv.black()


def wipe(cv, color, dt=0.045, trail=3, reverse=False):
    """A colour front sweeps column by column with a short fading trail."""
    cols = range(MAXCOL - 1, -1, -1) if reverse else range(MAXCOL)
    for c in cols:
        frame = {}
        for k in range(trail):
            cc = c + k if reverse else c - k
            if not 0 <= cc < MAXCOL:
                continue
            f = (trail - k) / trail
            for r in range(NROWS):
                p = pos(r, cc)
                if p is not None:
                    frame[p] = dim(color, f)
        cv.show(frame)
        cv.sleep(dt)
    cv.black()


def drum_path(span=DRUM_SPAN, step=2):
    """Every position the drumming hand visits, in order: serpentine down the
    three letter rows, then back up.

    Deterministic on purpose. The first version random-walked the row index with
    clamping at the ends, which looks reasonable and is not: from the top row two
    of three outcomes keep it there, the hand moves only once per ~0.9 s, and so
    over any short window it sits between the home and bottom rows and never
    climbs to QWERTY at all. Coverage you only get on average is not coverage.
    """
    path = []
    for i, (row, lo, hi) in enumerate(ALPHA_SPANS):
        last = max(lo, hi - span + 1)   # rightmost start that still fits the hand
        cols = list(range(lo, last + 1, step))
        if cols[-1] != last:
            cols.append(last)           # else the row's last letters (L, M) never
                                        # light — the stride steps over them
        if i % 2:                       # serpentine, so the hand slides rather
            cols.reverse()              # than jumping back across the row
        path += [(row, c) for c in cols]
    return path + list(reversed(path))[1:-1]     # ...and back up, continuously


DRUM_CYCLE_SECS = len(drum_path()) * DRUM_SPAN * DRUM_CYCLES * DRUM_DT

_ROW_INDEX = {row: i for i, (row, _, _) in enumerate(ALPHA_SPANS)}


def drum(cv, color, secs, span=DRUM_SPAN, dt=DRUM_DT, cycles=DRUM_CYCLES):
    """Adjacent letter keys tapping in sequence — impatient fingers — with the
    hand travelling the alpha block instead of staying put.

    A fixed cluster of four keys on an 83-key board is too quiet to catch in
    peripheral vision, which is the whole job of a status light. Moving the hand
    fixes that without losing the character: it still reads as drumming, but the
    motion covers the middle of the board, so it reaches the corner of your eye
    wherever you happen to be looking.
    """
    path = drum_path(span)
    hand = 0
    row, col = path[0]
    _, _, hi = ALPHA_SPANS[_ROW_INDEX[row]]
    end = time.time() + secs
    i = 0
    while time.time() < end:
        cells = [(row, col + k) for k in range(span) if col + k <= hi]
        frame = {}
        for k, cell in enumerate(cells):
            p = pos(*cell)
            if p is not None:
                frame[p] = color if k == i % len(cells) else dim(color, 0.12)
        cv.show(frame)
        cv.sleep(dt)
        i += 1
        if i % (len(cells) * cycles) == 0:
            hand = (hand + 1) % len(path)
            row, col = path[hand]
            _, _, hi = ALPHA_SPANS[_ROW_INDEX[row]]
    cv.black()


def burst(cv, color, center=BOARD_CENTRE, rings=7, dt=0.055):
    """An expanding ring from one key, fading as it grows."""
    cr, cc = center
    for k in range(rings):
        frame = {}
        for r in range(NROWS):
            for c in range(ROW_LENS[r]):
                if max(abs(r - cr), abs(c - cc)) == k:   # square rings: cheap,
                    frame[pos(r, c)] = dim(color,        # and reads fine
                                           1.0 - k / rings)
        cv.show(frame)
        cv.sleep(dt)
    cv.black()


def glow_decay(cv, color, secs, elapsed=0.0, from_f=0.35, dt=0.5, curve=0.6):
    """A dim glow decaying to nothing — the fade IS the timer.

    Brightness says how long ago it finished: bright means just now, faint means
    a while back, dark means the window closed and the state retired itself.

    `elapsed` anchors the decay to the ORIGINAL event rather than to worker
    start. If a higher-priority state preempts this one and then clears, the
    resumed worker has to pick the glow up where wall-clock left it — otherwise a
    `done` that spent 50 s hidden behind an `attention` restarts at full
    brightness and claims the turn just finished.

    `curve` < 1 holds brightness up early and spends the tail dim, which matters
    because there is no gamma correction anywhere in the engine: a straight
    linear ramp perceptually vanishes about a third of the way in.
    """
    end = time.time() + max(0.0, secs - elapsed)
    while True:
        rem = end - time.time()
        if rem <= 0:
            break
        cv.fill(dim(color, from_f * ((rem / secs) ** curve)))
        cv.sleep(min(dt, rem))
    cv.fill((0, 0, 0))
