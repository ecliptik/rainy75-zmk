#!/usr/bin/env python3
"""
Green picker for the Rainy 75 "YubiKey wants a touch" indicator
(needs firmware with CONFIG_RGB_MGMT=y).

Two things to choose, so there are two modes:

    python3 reverse/tools/green_picker.py hues     # which green?  (default)
    python3 reverse/tools/green_picker.py rates    # how fast should it blink?
    python3 reverse/tools/green_picker.py both     # hues, then rates

`hues` shows 10 greens as a STEADY fill, 4 s each with 1.5 s of black between, so
your eye judges the colour itself without the blink distracting from it.
`rates` then blinks your chosen green (or the default) at 6 speeds so you can
pick the tempo separately. Note the numbers you like and report them.

Unlike orange_picker.py this drives the Rainy75 client library in-process rather
than shelling out to rainy75_rgb.py per frame — a subprocess per frame cannot
blink evenly, and it re-opens the serial port every time.

Port contention: a running rainy75-think worker holds the port exclusively
(TIOCEXCL), so this waits for it. If it can't get in, run `rainy75-think reset`
in another terminal first. Ctrl-C always restores the board.
"""
import argparse
import importlib.util
import os
import sys
import time

TOOLDIR = os.path.dirname(os.path.abspath(__file__))

# All at full saturation on the green axis, walked from yellow-green round to
# blue-green, plus two deliberately dimmer emeralds at the end: "too harsh" is a
# brightness complaint as often as a hue one, and 0x00ff.. neon green is the
# single most eye-searing thing 83 LEDs can do.
HUES = [
    ("aaff00", "yellow-green (limey)"),
    ("6bff00", "chartreuse  <- current default"),
    ("2bff00", "bright lime green"),
    ("00ff00", "pure neon green (harshest)"),
    ("00ff40", "green, faint cyan lean"),
    ("00ff64", "spring green"),
    ("00ff8c", "mint / medium spring green"),
    ("00ffaa", "aquamarine (greenest cyan)"),
    ("00e05a", "emerald, slightly dimmed"),
    ("00b43c", "forest emerald (dimmest)"),
]

# Half-period in seconds -> full blink cycle is 2x this. 0.175 is what the first
# implementation shipped with and read as too fast.
RATES = [
    (0.175, "current default - fast alarm"),
    (0.25,  "brisk"),
    (0.35,  "steady"),
    (0.50,  "calm - one blink per second"),
    (0.70,  "slow pulse"),
    (1.00,  "very slow - almost a heartbeat"),
]

HUE_SECS = 4.0
GAP_SECS = 1.5
RATE_SECS = 5.0

ACQUIRE_TIMEOUT_S = 20


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
    return tuple(int(hex_[i:i + 2], 16) for i in (0, 2, 4))


def show_hues(kb):
    print(f"\n=== HUES: {len(HUES)} greens, {HUE_SECS:g}s each, "
          f"{GAP_SECS:g}s black between ===\n")
    for i, (hex_, name) in enumerate(HUES, 1):
        print(f"  {i:>2}/{len(HUES)}   #{hex_}   {name}", flush=True)
        kb.fill(_rgb(hex_))
        time.sleep(HUE_SECS)
        kb.fill((0, 0, 0))
        time.sleep(GAP_SECS)


def show_rates(kb, hex_):
    print(f"\n=== RATES: blinking #{hex_}, {RATE_SECS:g}s per speed ===\n")
    color = _rgb(hex_)
    for i, (dt, name) in enumerate(RATES, 1):
        hz = 1.0 / (2 * dt)
        print(f"  {i}/{len(RATES)}   half-period {dt:.3g}s  "
              f"({hz:.2f} Hz)  {name}", flush=True)
        end = time.time() + RATE_SECS
        on = True
        while time.time() < end:
            kb.fill(color if on else (0, 0, 0))
            time.sleep(dt)
            on = not on
        kb.fill((0, 0, 0))
        time.sleep(GAP_SECS)


def main():
    ap = argparse.ArgumentParser(
        description="Pick the green (and blink speed) for the YubiKey indicator")
    ap.add_argument("mode", nargs="?", default="hues",
                    choices=("hues", "rates", "both"))
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--color", default="6bff00", metavar="RRGGBB",
                    help="green to use for `rates` (default 6bff00, chartreuse)")
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

    try:
        if args.mode in ("hues", "both"):
            show_hues(kb)
        if args.mode in ("rates", "both"):
            show_rates(kb, args.color.lstrip("#"))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        try:
            kb.clear()        # back to the normal effect
        except Exception:
            pass
        try:
            kb.close()
        except Exception:
            pass

    print("\nDone — board back to normal.")
    if args.mode in ("hues", "both"):
        print("Which HUE number did you like?  (then: green_picker.py rates "
              "--color <that hex>)")
    if args.mode in ("rates", "both"):
        print("Which RATE number felt right?")


if __name__ == "__main__":
    main()
