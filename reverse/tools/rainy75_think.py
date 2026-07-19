#!/usr/bin/env python3
"""
Status indicator for the Rainy 75 (needs CONFIG_RGB_MGMT=y firmware).

Shows what Claude Code is doing on the board, then clears when it's done. Two
looks: a slow orange breathe while working, and a cyan comet that walks the
board (a snake weaving down every row) when Claude needs you (a question or an
authorization).

MULTIPLE LOCAL SESSIONS share one keyboard, so state is arbitrated, not
last-writer-wins. Each Claude Code session records its own desired state, keyed
by the session_id the hook passes on stdin. The board shows the highest-priority
state across all live sessions:

    attention (cyan)  >  think (orange)  >  nothing (clear)

so one session finishing never clears another's indicator, and one session
waiting on you is never hidden by another that's just working. All state changes
take an exclusive lock, so concurrent hooks can't race into orphaned workers.

Driven by Claude Code hooks (JSON on stdin):

    UserPromptSubmit                 ->  start       (this session: working)
    PostToolUse / PostToolUseFailure ->  start       (resumed working -> orange)
    Notification (permission types)  ->  attention   (this session: needs you)
    Stop / SessionEnd                ->  stop        (this session: done)
    (maintenance)                    ->  reset       (kill all workers + clear)

Only local sessions drive the board here; the remote-forward paths in the
rainy75-think wrapper are separate and left alone. Safe by design: no keyboard
-> every command is a no-op, a 15-min worker timeout and `reset` are backstops,
and the firmware's Fn+RGB escape hatch clears host mode.
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
# "attention" = a cyan comet walking the board (waiting on you, see _run_snake —
# only "color" is used for it). Priority high -> low.
MODES = {
    "think":     {"color": (0xFF, 0x3C, 0x00), "breath": 1.8, "min_f": 0.05},
    "attention": {"color": (0x00, 0xE0, 0xFF)},
}
PRIORITY = ("attention", "think")
DEFAULT_MODE = "think"

STEPS = 16                   # fade steps each direction
MAX_SECS = 900               # safety: worker auto-stops after 15 min
SESSION_TTL = 1800           # prune a session's state after 30 min idle (crash cleanup)

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
    try:
        kb.fill((0, 0, 0))             # clean black canvas (enter host mode)
    except Exception:
        return
    while not stop["v"] and time.time() < deadline:
        frame = {}
        for k in range(SNAKE_LEN):     # head bright, trail fading
            f = shades[k]
            frame[path[(head - k) % length]] = (int(r * f), int(g * f), int(b * f))
        frame[path[(head - SNAKE_LEN) % length]] = (0, 0, 0)   # key leaving the tail
        try:
            kb.set_positions(frame)
        except Exception:
            break
        time.sleep(SNAKE_DT)
        head = (head + 1) % length


def _run_breathe(kb, params, stop):
    r, g, b = params["color"]
    min_f = params["min_f"]
    ramp = list(range(STEPS + 1)) + list(range(STEPS - 1, -1, -1))
    dt = (params["breath"] / 2) / STEPS
    deadline = time.time() + MAX_SECS
    while not stop["v"] and time.time() < deadline:
        for i in ramp:
            if stop["v"]:
                break
            f = min_f + (1.0 - min_f) * (i / STEPS)
            try:
                kb.fill((int(r * f), int(g * f), int(b * f)))
            except Exception:
                stop["v"] = True
                break
            time.sleep(dt)


def worker(mode):
    port = _find_port()
    if not port:
        return
    try:
        kb = _load_client()(port)
    except Exception:
        return

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))

    try:
        if mode == "attention":
            _run_snake(kb, MODES["attention"]["color"], stop)
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
        if age > SESSION_TTL:
            _rm(p)
            continue
        val = _read_str(p)
        if val in MODES:
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
        print("usage: rainy75_think.py start|attention|stop|reset  (hook JSON on stdin)",
              file=sys.stderr)
        sys.exit(2)
