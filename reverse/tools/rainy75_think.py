#!/usr/bin/env python3
"""
Status indicator for the Rainy 75 (needs CONFIG_RGB_MGMT=y firmware).

Shows what Claude Code is doing on the board, then clears when it's done, and
flashes when the YubiKey wants a touch. Three looks: a slow orange breathe while
working, a cyan comet that walks the board (a snake weaving down every row) when
Claude needs you (a question or an authorization), and a hard green blink while a
hardware token is waiting on a fingertip.

MULTIPLE LOCAL SESSIONS share one keyboard, so state is arbitrated, not
last-writer-wins. Each Claude Code session records its own desired state, keyed
by the session_id the hook passes on stdin. The board shows the highest-priority
state across all live sessions:

    touch (green)  >  attention (cyan)  >  think (orange)  >  nothing (clear)

so one session finishing never clears another's indicator, one session waiting on
you is never hidden by another that's just working, and a blocked hardware token
outranks both (it's blocking real work, and it clears in seconds). All state
changes take an exclusive lock, so concurrent hooks can't race into orphaned
workers.

Driven by Claude Code hooks (JSON on stdin):

    UserPromptSubmit                 ->  start       (this session: working)
    PostToolUse / PostToolUseFailure ->  start       (resumed working -> orange)
    Notification (permission types)  ->  attention   (this session: needs you)
    Stop / SessionEnd                ->  stop        (this session: done)
    (maintenance)                    ->  reset       (kill all workers + clear)

And by the YubiKey wrappers, which are NOT Claude hooks and pass no stdin (see
TOUCH_SESSION; rainy75-yubikey wraps ssh-sk-helper for FIDO2/SSH,
rainy75-yubikey-scd relays Assuan for OpenPGP):

    token op begins                  ->  touch-start (green blink)
    token op ends / is cancelled     ->  touch-stop  (drop it, re-arbitrate)

Only local sessions drive the board here; the remote-forward paths in the
rainy75-think wrapper are separate and left alone. Safe by design: no keyboard
-> every command is a no-op, a 15-min worker timeout, MODE_TTL, and `reset` are
backstops, and the firmware's Fn+RGB escape hatch clears host mode.
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

STATE_DIR = os.environ.get("RAINY75_STATE_DIR", "/tmp/rainy75-think")
SESS_DIR = os.path.join(STATE_DIR, "sessions")
LOCK = os.path.join(STATE_DIR, "lock")
WORKER_PID = os.path.join(STATE_DIR, "worker.pid")
WORKER_MODE = os.path.join(STATE_DIR, "worker.mode")

# Per-mode look. "think" = slow orange breathe (working, see _run_breathe);
# "attention" = a cyan comet walking the board (waiting on you, see _run_snake);
# "touch" = a hard green blink (the YubiKey wants a touch, see _run_blink). The
# last two use only "color". Priority high -> low.
MODES = {
    "think":     {"color": (0xFF, 0x3C, 0x00), "breath": 1.8, "min_f": 0.05},
    "attention": {"color": (0x00, 0xE0, 0xFF)},
    "touch":     {"color": (0x6B, 0xFF, 0x00)},   # chartreuse, picked by eye
}
PRIORITY = ("touch", "attention", "think")
DEFAULT_MODE = "think"

# A hardware token waiting on a fingertip outranks everything: the operation is
# blocked until you touch it, it lasts seconds, and unlike the Claude states it
# is not tied to a session. Driven by the rainy75-yubikey wrappers, which signal
# touch-start/touch-stop under this fixed pseudo-session id — so the green shows
# even with no Claude session running, and arbitration restores whatever the
# board was doing (orange/cyan/nothing) the moment the touch completes.
TOUCH_SESSION = "yubikey"

STEPS = 16                   # fade steps each direction
MAX_SECS = 900               # safety: worker auto-stops after 15 min
SESSION_TTL = 1800           # prune a session's state after 30 min idle (crash cleanup)

# Per-mode override of SESSION_TTL. A stuck orange is cosmetic, but a stuck green
# lies about hardware state, so "touch" is pruned aggressively: the window is
# seconds, and its wrapper clears it from an EXIT trap. This is the backstop for
# the one case the trap can't cover — the wrapper being SIGKILLed mid-operation.
MODE_TTL = {"touch": 30}

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


def _find_port():
    for pat in ("/dev/cu.usbmodem*123301", "/dev/cu.usbmodem*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def _load_client():
    spec = importlib.util.spec_from_file_location(
        "rainy75_rgb", os.path.join(TOOLDIR, "rainy75_rgb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Rainy75


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
ROW_LENS = (15, 15, 14, 15, 14, 10)   # Rainy 75 rows, row-major (matches rainy75_rgb)
SNAKE_LEN = 6
SNAKE_DT = 0.05                        # seconds per one-key step


def _snake_path():
    """Boustrophedon order over all 83 keys, so the comet weaves down the board
    and wraps continuously."""
    path = []
    pos = 0
    for i, n in enumerate(ROW_LENS):
        seg = list(range(pos, pos + n))
        if i % 2:                      # every other row runs the other way
            seg.reverse()
        path.extend(seg)
        pos += n
    return path


def _run_snake(kb, color, stop):
    path = _snake_path()
    length = len(path)
    r, g, b = color
    shades = [(SNAKE_LEN - k) / SNAKE_LEN for k in range(SNAKE_LEN)]  # head -> tail
    deadline = time.time() + MAX_SECS
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


def _run_blink(kb, color, stop):
    on = True
    black = (0, 0, 0)
    deadline = time.time() + MAX_SECS
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


def _run_breathe(kb, params, stop):
    r, g, b = params["color"]
    min_f = params["min_f"]
    ramp = list(range(STEPS + 1)) + list(range(STEPS - 1, -1, -1))
    dt = (params["breath"] / 2) / STEPS
    deadline = time.time() + MAX_SECS
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

# "touch" retries far more eagerly: the whole window is a second or two, so a
# 1 s wait behind a briefly-busy port (TIOCEXCL, previous worker still closing)
# would show the green only after the touch is already done. Cheap, since these
# retries only happen while a worker is being replaced.
ACQUIRE_RETRY_FAST_S = 0.1


def _acquire(mode):
    """Open the keyboard, retrying while the port settles after a host wake."""
    deadline = time.time() + ACQUIRE_TIMEOUT_S
    retry = ACQUIRE_RETRY_FAST_S if mode == "touch" else ACQUIRE_RETRY_S
    client = _load_client()
    while True:
        port = _find_port()
        if port:
            try:
                return client(port)
            except Exception:
                pass        # busy (another worker), or still enumerating
        if time.time() >= deadline:
            return None
        time.sleep(retry)


def worker(mode):
    kb = _acquire(mode)
    if kb is None:
        return

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))

    try:
        if mode == "attention":
            _run_snake(kb, MODES["attention"]["color"], stop)
        elif mode == "touch":
            _run_blink(kb, MODES["touch"]["color"], stop)
        else:
            _run_breathe(kb, MODES.get(mode, MODES[DEFAULT_MODE]), stop)
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


def _live_states():
    """Read every session's desired state, pruning stale (crashed) ones."""
    states = {}
    now = time.time()
    try:
        names = os.listdir(SESS_DIR)
    except OSError:
        return states
    for name in names:
        p = os.path.join(SESS_DIR, name)
        try:
            age = now - os.stat(p).st_mtime
        except OSError:
            continue
        # Read the state before judging its age: the TTL is per-mode (MODE_TTL),
        # so which deadline applies isn't known until we know what it holds.
        val = _read_str(p)
        if val not in MODES:
            _rm(p)                      # unreadable/garbage -> not a live state
            continue
        if age > MODE_TTL.get(val, SESSION_TTL):
            _rm(p)
            continue
        states[name] = val
    return states


def _board_mode(states):
    vals = set(states.values())
    for m in PRIORITY:
        if m in vals:
            return m
    return None


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
        return

    if _alive(pid) and cur == target:
        return                          # already showing target -> no flicker
    if _find_port() is None:
        return                          # no keyboard -> no-op
    _kill(pid)                          # replace any worker in the other mode
    p = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_worker", target],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    _write(WORKER_PID, str(p.pid))
    _write(WORKER_MODE, target)


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
        worker(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODE)
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
        apply_state(TOUCH_SESSION, "touch")
        sys.exit(0)
    if cmd == "touch-stop":
        apply_state(TOUCH_SESSION, None)
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
    elif cmd == "stop":
        apply_state(sid, None)
    else:
        print("usage: rainy75_think.py start|attention|stop|reset  (hook JSON on stdin)\n"
              "       rainy75_think.py touch-start|touch-stop      (YubiKey; no stdin)",
              file=sys.stderr)
        sys.exit(2)
