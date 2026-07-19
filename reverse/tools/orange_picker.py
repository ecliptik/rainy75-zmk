#!/usr/bin/env python3
"""
Orange color picker for the Rainy 75 (needs firmware with CONFIG_RGB_MGMT=y).

Shows 10 orange-ish colors, 5 s each, with 2 s of LEDs-off between them so your
eye resets between colors. Prints the color it's currently displaying. Note the
number you like best.

Run:  python3 reverse/tools/orange_picker.py
      python3 reverse/tools/orange_picker.py --port /dev/cu.usbmodemXXXX   # optional
"""
import argparse
import glob
import subprocess
import sys
import time

TOOL = "/Users/micheal/git/rainy75-zmk/reverse/tools/rainy75_rgb.py"

# R=FF, B=00, sweeping the green channel up: less green = redder/less yellow.
COLORS = [
    ("ff2800", "deepest red-orange (almost red)"),
    ("ff3c00", "red-orange"),
    ("ff5000", "orange-red"),
    ("ff6400", "true orange (deep)"),
    ("ff7300", "true orange"),
    ("ff8000", "orange"),
    ("ff8c00", "darkorange (web)"),
    ("ff9900", "orange-amber"),
    ("ffaa00", "amber"),
    ("ffbb00", "amber-gold (yellowest)"),
]

COLOR_SECS = 5.0
GAP_SECS = 2.0


def find_port():
    for pattern in ("/dev/cu.usbmodem*123301", "/dev/cu.usbmodem*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port (default: auto-detect the Rainy)")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("Rainy 75 not found on USB. Plug it in, or pass --port.")
        sys.exit(1)

    def rgb(cmd):
        subprocess.run(["python3", TOOL, "--port", port] + cmd,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"Port: {port}")
    print(f"10 colors, {COLOR_SECS:g}s each, {GAP_SECS:g}s off between. "
          "Note the number you like.\n")
    try:
        for i, (hex_, name) in enumerate(COLORS, 1):
            print(f"  {i:>2}/10   #{hex_}   {name}", flush=True)
            rgb(["fill", "--color", hex_])
            time.sleep(COLOR_SECS)
            print("        (off)", flush=True)
            rgb(["fill", "--color", "000000"])   # LEDs off
            time.sleep(GAP_SECS)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        rgb(["clear"])   # back to the normal effect
    print("\nDone — board back to normal. Which number did you like?")


if __name__ == "__main__":
    main()
