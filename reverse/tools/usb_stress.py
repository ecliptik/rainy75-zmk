#!/usr/bin/env python3
"""On-demand CDC-wedge repro: trigger firmware re-attach cycles and race
SMP traffic against them, then report whether the wedge fired.

Sends flash_mgmt group 64 cmd 5 ({"n": cycles, "gap": ms}); the firmware
runs n usb_disable/usb_enable cycles on its system workqueue.  Meanwhile
this script hammers SMP echo probes so transfer cancels always race live
traffic — the exact burst that births the wedge.  Afterwards, sustained
probe failure = wedge (the watchdog will persist the ring with the
WQ_QSTAT verdict and self-heal; read it with usb_diag.py --saved).

Usage: ./usb_stress.py [cycles] [gap_ms]     (defaults: 5 cycles, 20000 ms)

Defaults are deliberately gentle: 20 cycles at 3 s apart also trips macOS's
own port-level enumeration backoff, which looks like a device wedge and
muddies the verdict (2026-07-24).  Keep the cycles slow enough that the host
fully re-enumerates in between.
"""

import importlib.util
import os
import subprocess
import sys
import time

TOOLDIR = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "rainy75_rgb", os.path.join(TOOLDIR, "rainy75_rgb.py"))
rgb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rgb)

FLASH_GROUP = 64
CMD_USB_STRESS = 5


def probe():
    """One SMP echo via a fresh connection; True if the board answered."""
    try:
        kb = rgb.Rainy75()
        rgb.RGB_GROUP = FLASH_GROUP
        # cmd 4 read with off=0 doubles as a harmless echo-equivalent
        kb._request(rgb.SMP_OP_READ, 4, [("off", rgb._cbor_uint(0))],
                    timeout=1.5)
        kb.close()
        return True
    except Exception:
        try:
            kb.close()
        except Exception:
            pass
        return False


def main():
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    gap_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 20000

    # LED workers hold the port exclusively; clear them for the run
    subprocess.run(["pkill", "-f", "rainy75_think.py _worker"],
                   capture_output=True)
    time.sleep(1)

    kb = rgb.Rainy75()
    rgb.RGB_GROUP = FLASH_GROUP
    kb._request(rgb.SMP_OP_WRITE, CMD_USB_STRESS,
                [("n", rgb._cbor_uint(cycles)), ("gap", rgb._cbor_uint(gap_ms))])
    kb.close()
    print(f"stress started: {cycles} re-attach cycles, {gap_ms} ms apart")

    # Race probes against the cycling; each cycle drops the connection, so
    # failures DURING the run are expected — only the tail matters.
    end = time.time() + (cycles * (gap_ms / 1000 + 2)) + 5
    ok = fail = 0
    while time.time() < end:
        if probe():
            ok += 1
            sys.stdout.write(".")
        else:
            fail += 1
            sys.stdout.write("x")
        sys.stdout.flush()
        time.sleep(1)
    print(f"\nrun window over ({ok} ok / {fail} failed probes during churn)")

    # Verdict: give the host a generous window to finish re-enumerating —
    # several seconds of silence right after churn is normal, so the old
    # "15 consecutive dead seconds" rule reported false wedges. The board is
    # only wedged if it never answers at all.
    alive_at = None
    for i in range(60):
        if probe():
            alive_at = i
            break
        time.sleep(1)

    if alive_at is None:
        print("VERDICT: WEDGED — CDC silent for 60 s after the run. The "
              "watchdog persists the ring and self-heals; then read: "
              "usb_diag.py --saved")
    else:
        print(f"VERDICT: survived {cycles} cycles (CDC answered {alive_at}s "
              f"after churn). Confirm with usb_diag.py — a clean run has no "
              f"WSTALL/STARVED events.")


if __name__ == "__main__":
    main()
