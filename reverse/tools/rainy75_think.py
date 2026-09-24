#!/usr/bin/env python3
"""
Status indicator for the Rainy 75 (needs CONFIG_RGB_MGMT=y firmware).

Shows what Claude Code is doing on the board, then clears when it's done, and
flashes when the YubiKey is blocking on you. Six looks, each a short ENTRANCE
into a quiet SUSTAIN — because a state that can sit for minutes has to be
watchable for minutes:

    think      orange breathe                     working
    attention  cyan comet weaving down every row  needs you (question / auth)
    touch      chartreuse 1 Hz blink              hardware token waiting
    error      crimson double flash -> flatline   turn died, go look
    stalled    gold wipe -> travelling drum       transient API failure, just wait
    done       emerald burst -> 60 s decaying glow finished

`error` and `stalled` both come from StopFailure, split by what you should do
about it: a rate limit means wait, a bad credential means get up. Collapsing them
would cry wolf on the common case. `done`'s glow decays across its minute, so
brightness reads as "how long ago" — and it retires itself when it reaches black.

MULTIPLE LOCAL SESSIONS share one keyboard, so state is arbitrated, not
last-writer-wins. Each Claude Code session records its own desired state, keyed
by the session_id the hook passes on stdin. The board shows the highest-priority
state across all live sessions:

    touch > attention > error > stalled > think > done > nothing (clear)

so one session finishing never clears another's indicator, one session waiting on
you is never hidden by another that's just working, and a blocked hardware token
outranks everything (it's blocking real work, and it clears the moment you deal
with it). `attention` outranks `error` deliberately: attention is a LIVE block
with Claude idling on you, while an error has already stopped. `done` sits at the
bottom as the only state that asks nothing of you. All state changes take an
exclusive lock, so concurrent hooks can't race into orphaned workers.

Driven by Claude Code hooks (JSON on stdin):

    UserPromptSubmit                 ->  start       (this session: working)
    PostToolUse / PostToolUseFailure ->  start       (resumed working -> orange)
    Notification (permission types)  ->  attention   (this session: needs you)
    StopFailure                      ->  fail        (error_type -> error/stalled)
    Stop                             ->  done        (finished -> decaying glow)
    SessionEnd                       ->  stop        (drop this session entirely)
    (maintenance)                    ->  reset       (kill all workers + clear)

And by the YubiKey wrappers, which are NOT Claude hooks and pass no stdin (see
TOUCH_SESSION; rainy75-yubikey wraps ssh-sk-helper for FIDO2/SSH,
rainy75-yubikey-scd relays Assuan for OpenPGP):

    token op begins                  ->  touch-start (green blink; refcounted)
    token op ends / is cancelled     ->  touch-stop  (last one out re-arbitrates)

Only local sessions drive the board here; the remote-forward paths in the
rainy75-think wrapper are separate and left alone. Safe by design: no keyboard
-> every command is a no-op, a per-mode worker deadline, MODE_TTL, and `reset`
are backstops, and the firmware's Fn+RGB escape hatch clears host mode.
"""
import fcntl
import glob
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time

TOOLDIR = os.path.dirname(os.path.abspath(__file__))
if TOOLDIR not in sys.path:
    sys.path.insert(0, TOOLDIR)         # `_worker` re-execs this file directly
import rainy75_anim as anim             # noqa: E402  (needs the path above)

STATE_DIR = os.environ.get("RAINY75_STATE_DIR", "/tmp/rainy75-think")
SESS_DIR = os.path.join(STATE_DIR, "sessions")
LOCK = os.path.join(STATE_DIR, "lock")
WORKER_PID = os.path.join(STATE_DIR, "worker.pid")
WORKER_MODE = os.path.join(STATE_DIR, "worker.mode")
# Which Stop the running `done` glow is anchored to. Held separately from
# worker.mode so the mode comparison in _drive() stays a plain equality test.
WORKER_ANCHOR = os.path.join(STATE_DIR, "worker.anchor")

# Per-mode look. Colours live in rainy75_anim.PALETTE, picked on real hardware
# with state_picker.py; only the breathe carries extra parameters. The verb for
# the YubiKey state stays "touch" because the wrappers and their CLI contract are
# named for it. Priority high -> low.
MODES = {
    "think":     {"color": anim.PALETTE["think"], "breath": 1.8, "min_f": 0.05},
    "attention": {"color": anim.PALETTE["attention"]},
    "touch":     {"color": anim.PALETTE["touch"]},
    "error":     {"color": anim.PALETTE["error"]},
    "stalled":   {"color": anim.PALETTE["stalled"]},
    "done":      {"color": anim.PALETTE["done"]},
}
PRIORITY = ("touch", "attention", "error", "stalled", "think", "done")
DEFAULT_MODE = "think"

# StopFailure error_type -> which state. Transient failures mean "wait, it may
# retry" and terminal ones mean "get up and deal with it"; one light for both
# would cry wolf on rate limits, which are the common case by a distance.
# Anything unrecognised (including a missing error_type, e.g. a forwarded call
# that carried no stdin) is treated as terminal — the louder, safer default.
TRANSIENT_ERRORS = {"rate_limit", "overloaded", "server_error"}

# A hardware token waiting on a fingertip outranks everything: the operation is
# blocked until you touch it, it lasts seconds, and unlike the Claude states it
# is not tied to a session. Driven by the rainy75-yubikey wrappers, which signal
# touch-start/touch-stop under this fixed pseudo-session id — so the green shows
# even with no Claude session running, and arbitration restores whatever the
# board was doing (orange/cyan/nothing) the moment the touch completes. Because
# they all share the one id, its state is refcounted (see adjust_refcount).
TOUCH_SESSION = "yubikey"

STEPS = 16                   # fade steps each direction
MAX_SECS = 900               # safety: worker auto-stops after 15 min (see _worker_secs)
SESSION_TTL = 1800           # prune a session's state after 30 min idle (crash cleanup)

# Per-mode override of SESSION_TTL. A stuck orange is cosmetic, but a stuck green
# lies about hardware state, so "touch" is pruned far harder than a Claude session.
#
# 180 s, not the 30 s this started at. 30 s was sized for a FIDO touch, which the
# token itself bounds at ~15-30 s — but the wait that actually dominates is a
# gpg-agent pinentry prompt (the OpenPGP card path, see the scdaemon relay), and
# that one is unbounded: it sits there until you notice it, which is the entire
# reason the board lights up. A 30 s cap went dark mid-prompt, precisely when the
# indicator was doing its job. The relay sends touch-stop itself even when
# scdaemon dies mid-command, so this only has to backstop the relay being
# SIGKILLed — 180 s bounds a wedged green without truncating an honest wait.
#
# `error`/`stalled` are sticky — an error you didn't see is an error you'll
# repeat — but they must still expire, so a crashed session cannot leave a red
# board forever.
#
# `error` sits at 120 s: a flatlined board that outstays the moment reads as
# broken lighting rather than as a signal, and the turn it refers to is already
# on screen. The cost is real and deliberate — an error that lands while you are
# away from the desk is gone by the time you return — so if a missed failure
# ever bites, this is the number to raise. `stalled` stays long: it means the
# turn may still be retrying, so the light is reporting a live condition rather
# than a past one.
#
# `done` takes exactly its animation length. The decay and the deadline are the
# same number by construction: when the glow reaches black the state is gone.
MODE_TTL = {"touch": 180, "error": 120, "stalled": 900, "done": int(anim.DONE_SECS)}


def _worker_secs(mode):
    """How long a worker may run, time spent acquiring the port included.

    A worker must not outlive the state that spawned it. MODE_TTL alone doesn't
    give you that: it is only consulted from _live_states(), which only runs when
    a command arrives, so it bounds the *state file* and not the *light*. In the
    exact case it was written for — the wrapper SIGKILLed, so touch-stop never
    fires — nothing re-arbitrates, and the board would blink on to MAX_SECS, long
    past the point the state was declared untrustworthy. Deriving the worker's
    deadline from the same number retires the light and the state together,
    without needing anything else to happen: raise MODE_TTL and the light follows.
    """
    return min(MAX_SECS, MODE_TTL.get(mode, MAX_SECS))


# Notification types that mean "Claude needs you" -> attention. Others
# (idle_prompt, auth_success, ...) don't light the board.
ATTENTION_NTYPES = {"permission_prompt", "agent_needs_input", "elicitation_dialog"}


# --------------------------------------------------------------------------
# small fs helpers
# --------------------------------------------------------------------------

def _read_int(path):
    try:
        return int(open(path).read().strip())
    except (OSError, ValueError):
        return None


def _read_str(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None


def _write(path, s):
    try:
        with open(path, "w") as f:
            f.write(s)
    except OSError:
        pass


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# finding the keyboard
#
# rainy75_rgb.find_port() decides, by USB product name: taking the first
# CDC-ACM node could open some other device (a monitor's control interface, a
# dev board). This side only caches its macOS answer, which costs an ioreg call.
# --------------------------------------------------------------------------

PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/ttyACM*")
PORT_CACHE = os.path.join(STATE_DIR, "port.json")
_rgb_module = None


def _load_rgb():
    global _rgb_module
    if _rgb_module is None:
        spec = importlib.util.spec_from_file_location(
            "rainy75_rgb", os.path.join(TOOLDIR, "rainy75_rgb.py"))
        _rgb_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_rgb_module)
    return _rgb_module


def _find_port():
    """The Rainy 75's serial device, or None.

    RAINY75_PORT, when set, is taken as is (the rainy75-think wrapper passes the
    port it already found). On macOS the answer costs a few hundred ms of ioreg
    and _acquire() asks every 0.1 s, so it is cached per set of usbmodem nodes
    (plugging or unplugging anything changes the set). Linux is not cached: the
    sysfs lookup is cheap, and ttyACM names are reused, so a different device
    can reappear under the keyboard's old name.
    """
    env = os.environ.get("RAINY75_PORT")
    if env:
        return env if os.path.exists(env) else None
    nodes = sorted(n for pat in PORT_GLOBS for n in glob.glob(pat))
    if not nodes:
        return None
    macos = any(n.startswith("/dev/cu.") for n in nodes)
    if macos:
        try:
            with open(PORT_CACHE) as f:
                cached = json.load(f)
            if cached.get("nodes") == nodes:
                return cached.get("port")
        except (OSError, ValueError, AttributeError):
            pass
    port = _load_rgb().find_port()
    if macos:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = "%s.%d" % (PORT_CACHE, os.getpid())
            with open(tmp, "w") as f:
                json.dump({"nodes": nodes, "port": port}, f)
            os.replace(tmp, PORT_CACHE)
        except OSError:
            pass
    return port


def _load_client():
    return _load_rgb().Rainy75


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid):
    """SIGTERM a worker and wait (up to ~1.5 s) for it to clear + exit."""
    if not _alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(30):
        if not _alive(pid):
            break
        time.sleep(0.05)


# --------------------------------------------------------------------------
# the breather / snake (one process; drives the whole board for a single mode)
# --------------------------------------------------------------------------

# "attention" renders as a comet that walks a serpentine path across the board:
# row 0 left->right, row 1 right->left, ... down every row, then wraps to the
# top. SNAKE_LEN keys are lit in a bright-to-dim gradient for fluid motion.
SNAKE_LEN = 6
SNAKE_DT = 0.05                        # seconds per one-key step


def _run_snake(kb, color, stop, deadline):
    path = anim.snake_path()            # board geometry lives in rainy75_anim
    length = len(path)
    r, g, b = color
    shades = [(SNAKE_LEN - k) / SNAKE_LEN for k in range(SNAKE_LEN)]  # head -> tail
    head = 0
    misses = 0
    try:
        kb.fill((0, 0, 0))             # clean black canvas (enter host mode)
    except Exception:
        pass                           # the frame loop below retries
    while not stop["v"] and time.time() < deadline:
        frame = {}
        for k in range(SNAKE_LEN):     # head bright, trail fading
            f = shades[k]
            frame[path[(head - k) % length]] = (int(r * f), int(g * f), int(b * f))
        frame[path[(head - SNAKE_LEN) % length]] = (0, 0, 0)   # key leaving the tail
        try:
            kb.set_positions(frame)
            misses = 0
        except Exception:
            misses += 1                # a blip is not a dead link (see breathe)
            if misses >= MAX_CONSECUTIVE_MISSES:
                break
            time.sleep(0.2)
            continue
        time.sleep(SNAKE_DT)
        head = (head + 1) % length


# Dropped frames tolerated before an animation concludes the link is gone.
MAX_CONSECUTIVE_MISSES = 5

# "touch" renders as a hard on/off blink of the whole board — deliberately not a
# breathe. The token is *blocking* an operation, so this has to read as an alarm
# at the edge of vision and be unmistakable against the smooth orange working
# glow; a square wave is the least ambiguous signal the board can make.
# Half-period, so a full cycle is 2x this. Tuned by eye with green_picker.py:
# 0.175 (~2.9 Hz) was a frantic strobe across 83 keys and 0.35 (~1.4 Hz) was
# still busy. One blink per second reads as "look at me" without being
# unpleasant to sit beside for the seconds a touch takes.
BLINK_DT = 0.5                         # -> 1 Hz


def _run_blink(kb, color, stop, deadline):
    on = True
    black = (0, 0, 0)
    misses = 0
    while not stop["v"] and time.time() < deadline:
        try:
            kb.fill(color if on else black)
            misses = 0
        except Exception:
            misses += 1                # a blip is not a dead link (see breathe)
            if misses >= MAX_CONSECUTIVE_MISSES:
                break
            time.sleep(0.2)
            continue
        time.sleep(BLINK_DT)
        on = not on


def _run_breathe(kb, params, stop, deadline):
    r, g, b = params["color"]
    min_f = params["min_f"]
    ramp = list(range(STEPS + 1)) + list(range(STEPS - 1, -1, -1))
    dt = (params["breath"] / 2) / STEPS
    misses = 0
    while not stop["v"] and time.time() < deadline:
        for i in ramp:
            if stop["v"]:
                break
            f = min_f + (1.0 - min_f) * (i / STEPS)
            try:
                kb.fill((int(r * f), int(g * f), int(b * f)))
                misses = 0
            except Exception:
                # One dropped frame is not a dead link: a host suspend/resume
                # or a busy port stalls a request briefly. Only quit once the
                # link stays unusable, so a blip does not end the animation.
                misses += 1
                if misses >= MAX_CONSECUTIVE_MISSES:
                    stop["v"] = True
                    break
                time.sleep(0.2)
                continue
            time.sleep(dt)


# The keyboard is not reachable the instant a host wakes: it re-presents itself
# and the port re-enumerates, which takes seconds. A worker that gives up on the
# first failure leaves the board dark until some later hook happens to fire —
# the "pulse only came back when you ran a command" symptom. Keep trying for
# long enough to cover a wake, then give up so a genuinely absent keyboard does
# not leave a process lingering.
ACQUIRE_TIMEOUT_S = 25
ACQUIRE_RETRY_S = 1.0

# "touch" retries far more eagerly, and concedes far sooner. The whole window is
# a second or two, so a 1 s wait behind a briefly-busy port (TIOCEXCL, previous
# worker still closing) would show the green only after the touch is already
# done — and holding out for the full 25 s could win the port long after the
# token stopped waiting, lighting the board for an operation that no longer
# exists. Retry hard, then give up. Cheap, since these retries only happen while
# a worker is being replaced.
ACQUIRE_RETRY_FAST_S = 0.1
MODE_ACQUIRE_TIMEOUT_S = {"touch": 5}


def _acquire(mode, deadline):
    """Open the keyboard, retrying while the port settles after a host wake.

    Bounded by this mode's acquire ceiling and by `deadline`, the worker's whole
    lifetime budget — so a slow acquire eats into the animation rather than
    extending the worker past the point its state is still believable.
    """
    limit = min(deadline, time.time()
                + MODE_ACQUIRE_TIMEOUT_S.get(mode, ACQUIRE_TIMEOUT_S))
    retry = ACQUIRE_RETRY_FAST_S if mode == "touch" else ACQUIRE_RETRY_S
    client = _load_client()
    while True:
        port = _find_port()
        if port:
            try:
                return client(port)
            except Exception:
                pass        # busy (another worker), or still enumerating
        if time.time() >= limit:
            return None
        time.sleep(retry)


# --------------------------------------------------------------------------
# the new states (entrance -> sustain), rendered through rainy75_anim's Canvas
#
# The Canvas owns abort and dropped-frame handling for these, so they read as
# straight-line animation code: it slices every sleep to catch a SIGTERM inside a
# 5 s flatline gap, and raises Aborted once the link stays unusable. The three
# older renderers above predate it and keep their own hand-rolled versions.
# --------------------------------------------------------------------------

def _run_error(kb, color, stop, deadline):
    cv = anim.Canvas(kb, stop, deadline)
    anim.flash(cv, color)                              # entrance
    anim.flatline(cv, color, max(0.0, deadline - time.time()))   # sustain


def _run_stalled(kb, color, stop, deadline):
    cv = anim.Canvas(kb, stop, deadline)
    anim.wipe(cv, color)                               # entrance
    anim.drum(cv, color, max(0.0, deadline - time.time()))       # sustain


def _run_done(kb, color, stop, deadline, elapsed):
    cv = anim.Canvas(kb, stop, deadline)
    anchor = time.time() - elapsed     # when the Stop actually happened
    if elapsed < 1.0:
        anim.burst(cv, color, times=anim.DONE_BURSTS)
                                       # entrance — only if we're at the start.
                                       # Resuming a glow that a higher-priority
                                       # state hid for 40 s must not re-announce
                                       # a completion that already happened.
    # Re-derive elapsed so the bursts are spent INSIDE the window: `done` must
    # last DONE_SECS from the Stop, not DONE_SECS plus however long the entrance
    # took.
    anim.glow_decay(cv, color, anim.DONE_SECS, elapsed=time.time() - anchor)


def worker(mode, elapsed=0.0):
    # One budget for the whole worker, acquire included (see _worker_secs).
    # `done` is additionally bounded by whatever is left of its decay window.
    secs = _worker_secs(mode)
    if mode == "done":
        secs = min(secs, max(0.0, anim.DONE_SECS - elapsed))
        if secs <= 0:
            return                     # window already closed; nothing to show
    deadline = time.time() + secs
    kb = _acquire(mode, deadline)
    if kb is None:
        return

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))

    try:
        if mode == "attention":
            _run_snake(kb, MODES["attention"]["color"], stop, deadline)
        elif mode == "touch":
            _run_blink(kb, MODES["touch"]["color"], stop, deadline)
        elif mode == "error":
            _run_error(kb, MODES["error"]["color"], stop, deadline)
        elif mode == "stalled":
            _run_stalled(kb, MODES["stalled"]["color"], stop, deadline)
        elif mode == "done":
            _run_done(kb, MODES["done"]["color"], stop, deadline, elapsed)
        else:
            _run_breathe(kb, MODES.get(mode, MODES[DEFAULT_MODE]), stop, deadline)
    except anim.Aborted:
        pass                           # SIGTERM, deadline, or a dead link
    finally:
        try:
            kb.clear()
        except Exception:
            pass
        try:
            kb.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# per-session state + arbitration (all under an exclusive lock)
# --------------------------------------------------------------------------

class _Lock:
    def __enter__(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            os.chmod(STATE_DIR, 0o700)
        except OSError:
            pass
        self.fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _sess_path(sid):
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:128] or "cli"
    return os.path.join(SESS_DIR, safe)


def _parse_state(raw):
    """A state file holds `mode`, or `mode <refcount>` for a refcounted session
    (see adjust_refcount). Returns (mode, count), or (None, 0) if the file is
    empty, garbage, or names a mode this version doesn't know."""
    parts = (raw or "").split()
    if not parts or parts[0] not in MODES:
        return None, 0
    if len(parts) == 1:
        return parts[0], 1              # plain state == one holder
    try:
        count = int(parts[1])
    except ValueError:
        return None, 0
    return (parts[0], count) if count > 0 else (None, 0)


def _read_live_state(path):
    """(mode, refcount) for one session file, or (None, 0) if it is missing,
    stale, or unparseable — deleting it in the latter two cases.

    Shared by _live_states() and adjust_refcount() so the two can never disagree
    about what is still live: a refcount inherited from a wrapper that died is
    exactly as untrustworthy as the mode beside it, and must not be built on.
    """
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        return None, 0
    # Read the state before judging its age: the TTL is per-mode (MODE_TTL), so
    # which deadline applies isn't known until we know what the file holds.
    mode, count = _parse_state(_read_str(path))
    if mode is None:
        _rm(path)                       # unreadable/garbage -> not a live state
        return None, 0
    if age > MODE_TTL.get(mode, SESSION_TTL):
        _rm(path)
        return None, 0
    return mode, count


def _live_states():
    """Read every session's desired state, pruning stale (crashed) ones."""
    states = {}
    try:
        names = os.listdir(SESS_DIR)
    except OSError:
        return states
    for name in names:
        mode, _ = _read_live_state(os.path.join(SESS_DIR, name))
        if mode:
            states[name] = mode         # arbitration cares about the mode only
    return states


def _board_mode(states):
    vals = set(states.values())
    for m in PRIORITY:
        if m in vals:
            return m
    return None


def _done_anchor():
    """When the `done` glow started — the mtime of the most recent `done` state.

    Newest wins: if two sessions have finished, brightness should track the one
    that just did, not the one from 50 s ago. Returns None if nothing is done.
    """
    newest = None
    try:
        names = os.listdir(SESS_DIR)
    except OSError:
        return None
    for name in names:
        path = os.path.join(SESS_DIR, name)
        mode, _ = _read_live_state(path)
        if mode != "done":
            continue
        try:
            ts = os.stat(path).st_mtime
        except OSError:
            continue
        if newest is None or ts > newest:
            newest = ts
    return newest


def _drive(target):
    """Make the single worker show `target` (or clear if None)."""
    pid = _read_int(WORKER_PID)
    cur = _read_str(WORKER_MODE)

    if target is None:
        if _alive(pid):
            _kill(pid)                  # worker clears on exit
        else:
            port = _find_port()         # no worker: clear directly in case lit
            if port:
                try:
                    _load_client()(port).clear()
                except Exception:
                    pass
        _rm(WORKER_PID)
        _rm(WORKER_MODE)
        _rm(WORKER_ANCHOR)
        return

    # `done` fades in WALL-CLOCK time, not time-on-screen: the decay is anchored
    # to the Stop that started it, so a glow hidden behind an `attention` for 40 s
    # resumes with 20 s left rather than restarting bright and claiming the turn
    # just finished. A newer Stop is a different anchor and does restart it.
    anchor = _done_anchor() if target == "done" else None
    anchor_s = "" if anchor is None else "%.3f" % anchor
    elapsed = 0.0 if anchor is None else max(0.0, time.time() - anchor)

    if _alive(pid) and cur == target and (
            target != "done" or _read_str(WORKER_ANCHOR) == anchor_s):
        return                          # already showing target -> no flicker
    if _find_port() is None:
        return                          # no keyboard -> no-op
    _kill(pid)                          # replace any worker in the other mode
    p = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_worker", target,
         "%.3f" % elapsed],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    _write(WORKER_PID, str(p.pid))
    _write(WORKER_MODE, target)
    if target == "done":
        _write(WORKER_ANCHOR, anchor_s)
    else:
        _rm(WORKER_ANCHOR)


def apply_state(sid, state):
    """Record this session's desired state (or None to drop it), re-arbitrate."""
    os.makedirs(SESS_DIR, exist_ok=True)
    with _Lock():
        path = _sess_path(sid)
        if state is None:
            _rm(path)
        else:
            _write(path, state)
        _drive(_board_mode(_live_states()))


def adjust_refcount(sid, mode, delta):
    """Nest a shared state: +1 to assert it, -1 to release it, the board clearing
    only once the last holder has let go. Re-arbitrates either way.

    The YubiKey wrappers all signal under one fixed session id (TOUCH_SESSION),
    because a token touch is system-wide rather than per-session. A bare
    set/clear would then let whichever operation finished first switch the green
    off while another was still waiting on a fingertip — a dark board with the
    token blocked, which is the same lie about hardware the indicator exists to
    prevent, only inverted. FIDO and OpenPGP reach the key over separate
    interfaces and genuinely do overlap (an SSH push while a commit is being
    signed), so the nesting is not hypothetical.
    """
    os.makedirs(SESS_DIR, exist_ok=True)
    with _Lock():
        path = _sess_path(sid)
        cur, count = _read_live_state(path)
        if cur != mode:
            count = 0       # expired, or another mode's file -> start over at 0
        count += delta
        if count > 0:
            _write(path, f"{mode} {count}")
        else:
            _rm(path)       # also covers releasing when nothing is held (no-op)
        _drive(_board_mode(_live_states()))


def reset():
    """Kill every worker (tracked or orphaned), wipe state, clear the board."""
    try:
        subprocess.call(["pkill", "-f", "rainy75_think.py _worker"])
    except Exception:
        pass
    time.sleep(0.2)                     # let workers release the serial port
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    for legacy in ("/tmp/rainy75_think.pid", "/tmp/rainy75_think.mode"):
        _rm(legacy)
    port = _find_port()
    if port:
        try:
            _load_client()(port).clear()
        except Exception:
            pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _hook_input():
    """Parse the hook's JSON stdin (session_id, notification_type). Empty for
    a manual/tty invocation."""
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "_worker":
        try:
            _elapsed = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
        except ValueError:
            _elapsed = 0.0
        worker(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODE, _elapsed)
        sys.exit(0)
    if cmd == "reset":
        reset()
        sys.exit(0)

    # The YubiKey verbs are separate from start/stop because they act on the
    # fixed TOUCH_SESSION rather than the caller's session: a plain "stop" from a
    # Claude hook must not clear a pending token touch, and vice versa.
    #
    # Dispatched BEFORE _hook_input(): these are not Claude hooks and carry no
    # JSON, and their caller (rainy75-yubikey, wrapping ssh-sk-helper) is handed
    # a socketpair on stdin by ssh-agent. Reading stdin here would swallow bytes
    # of the live FIDO protocol. The wrapper also redirects stdin from /dev/null
    # for defence in depth, but this ordering is what makes it safe by design.
    if cmd == "touch-start":
        adjust_refcount(TOUCH_SESSION, "touch", +1)
        sys.exit(0)
    if cmd == "touch-stop":
        adjust_refcount(TOUCH_SESSION, "touch", -1)
        sys.exit(0)

    hook = _hook_input()
    sid = str(hook.get("session_id") or "cli")

    if cmd == "start":
        apply_state(sid, "think")
    elif cmd == "attention":
        ntype = str(hook.get("notification_type") or "")
        # A known non-attention notification (idle_prompt, auth_success) doesn't
        # light the board; an explicit/manual call (empty type) does.
        if ntype and ntype not in ATTENTION_NTYPES:
            sys.exit(0)
        apply_state(sid, "attention")
    elif cmd == "fail":
        # StopFailure. Claude Code does NOT also fire Stop for a failed turn, so
        # without this the session stays `think` and the board breathes orange at
        # a dead turn until MAX_SECS retires it a quarter of an hour later.
        etype = str(hook.get("error_type") or "")
        apply_state(sid, "stalled" if etype in TRANSIENT_ERRORS else "error")
    elif cmd == "done":
        apply_state(sid, "done")
    elif cmd == "stop":
        apply_state(sid, None)
    else:
        print("usage: rainy75_think.py start|attention|fail|done|stop|reset\n"
              "                                                   (hook JSON on stdin)\n"
              "       rainy75_think.py touch-start|touch-stop      (YubiKey; no stdin)",
              file=sys.stderr)
        sys.exit(2)
