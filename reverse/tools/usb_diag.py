#!/usr/bin/env python3
"""Read the B91 USB diagnostic event ring over SMP (flash_mgmt group 64, cmd 4).

The ring lives in .noinit SRAM on the keyboard and survives cable pulls and
re-attach cycles (the MCU runs from battery), so after a CDC wedge recovers —
by replug or by the firmware's own starvation watchdog — this shows the USB
event sequence that preceded and followed the wedge.

Usage: ./usb_diag.py [port]
"""

import importlib.util
import os
import struct
import sys

TOOLDIR = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    "rainy75_rgb", os.path.join(TOOLDIR, "rainy75_rgb.py"))
rgb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rgb)

FLASH_GROUP = 64
CMD_USB_DIAG = 4
PAGE = 32

# Mirrors enum b91_usb_diag_code in usb_dc_b91.c
CODES = {
    1: "STATUS",
    2: "SETUP",
    3: "EP_CFG",
    4: "EP_EN",
    5: "EP_DIS",
    6: "ARM",
    7: "STARVED",
    8: "DETACH",
    9: "WQ_STUCK",
    10: "WQ_STATE",
    11: "WQ_PEND2",
    12: "WQ_QSTAT",
    13: "SLOT",
    14: "UNCONF",
    15: "WSTALL",
    16: "WDETAIL",
    17: "WQPTR",
    18: "WAKE_REQ",
    19: "SUSP_POLL",
    32: "RGB_STATE",
}

# Low byte of the slot's int status. Values are THIS libc's errno numbers
# (picolibc/newlib): ECANCELED is 140, not Linux's 125 — (int8_t)-140 = 0x74,
# which an earlier decoder misread as corruption.
STATUS_NAMES = {0: "free",
                (256 - 16) & 0xFF: "-EBUSY(live)",
                (256 - 140) & 0xFF: "-ECANCELED",
                (256 - 22) & 0xFF: "-EINVAL",
                (256 - 12) & 0xFF: "-ENOMEM",
                (256 - 11) & 0xFF: "-EAGAIN",
                (256 - 19) & 0xFF: "-ENODEV"}

# Zephyr usb_dc_status_code values (usb_dc.h)
STATUS = {0: "ERROR", 1: "RESET", 2: "CONNECTED", 3: "CONFIGURED",
          4: "DISCONNECTED", 5: "SUSPEND", 6: "RESUME", 7: "INTERFACE",
          8: "SET_HALT", 9: "CLEAR_HALT", 10: "SOF", 11: "UNKNOWN"}

SETUP_REQ = {1: "CLEAR_FEATURE", 3: "SET_FEATURE", 5: "SET_ADDRESS",
             9: "SET_CONFIGURATION", 11: "SET_INTERFACE"}


def describe(code, a, b):
    name = CODES.get(code, f"code{code}")
    if name == "STATUS":
        return f"STATUS {STATUS.get(a, a)}"
    if name == "SETUP":
        return f"SETUP {SETUP_REQ.get(a, a)} wValue={b}"
    if name == "EP_CFG":
        sram = "ENOMEM!" if b == 0xFFFF else f"sram@{b}"
        return f"EP_CFG ep=0x{a:02x} {sram}"
    if name in ("EP_EN", "EP_DIS"):
        return f"{name} ep=0x{a:02x}"
    if name == "ARM":
        state = {0: "UN-ARMED", 1: "re-armed", 2: "DISABLED (torn from EDP_EN)"}.get(a, a)
        extra = f" (was dead {b} ticks = {b // 2}s)" if a == 1 and b else ""
        return f"OUT {state}{extra}"
    if name == "STARVED":
        how = "EP disabled" if a & 0x80 else "un-armed"
        return f"STARVED ep={a & 0x7F} ({how}) after {b} ticks ({b // 2}s) -> recovery hook"
    if name == "DETACH":
        return "DETACH"
    if name == "WAKE_REQ":
        # bit0 attached (and therefore driven), bit1 driver suspend flag,
        # bit2 live MDEV suspend level. b = raw IRQ status/level register.
        att = "attached" if a & 1 else "DETACHED"
        sus = "susp-flag" if a & 2 else "no-flag"
        lvl = "susp-level" if a & 4 else "no-level"
        drove = " -> RESUME PULSE DRIVEN" if a & 1 else " -> refused (detached)"
        irqb = [n for m, n in [(0x80, "SUSPEND_O"), (0x40, "250US_O"),
                               (0x10, "250US_LVL"), (0x08, "RESET_LVL")]
                if b & m]
        return f"WAKE_REQ ({att}, {sus}, {lvl}; irq={'|'.join(irqb) or 'none'}){drove}"
    if name == "RGB_STATE":
        believed = bool(a & 1)
        pin_high = bool(a & 16)
        flags = [n for m, n in [(1, "rail-believed-on"), (2, "idle"),
                                (4, "host-mode"), (8, "effect-on"),
                                (16, "PC2-HIGH"), (32, "PC2-out-en"),
                                (64, "PC2-gpio-mode")] if a & m]
        verdict = ""
        if believed and not pin_high:
            verdict = "  <-- RAIL DIVERGENCE: loop thinks powered, pin is LOW (strip dark)"
        elif not believed and pin_high:
            verdict = "  (rail up while loop thinks off — benign)"
        return f"RGB_STATE {'|'.join(flags) or 'none'} tick={b << 4}{verdict}"
    if name == "SUSP_POLL":
        sigs = [n for m, n in [(1, "SUSPEND_O"), (2, "mdev-level"),
                               (4, "quiet"), (8, "configured")] if a & m]
        return f"SUSP_POLL {'|'.join(sigs) or 'none'} isr_delta={b}"
    if name == "WQ_STATE":
        # thread_state bits (kernel_structs.h): 1=dummy 2=pending 4=prestart
        # 8=dead 16=suspended 32=aborting 128=queued
        bits = [n for m, n in [(1, "dummy"), (2, "PENDING"), (4, "prestart"),
                               (8, "DEAD"), (16, "suspended"), (32, "aborting"),
                               (128, "queued")] if a & m]
        return (f"WQ_STATE thread_state=0x{a:02x} ({'|'.join(bits) or 'running?'}) "
                f"pended_on_low16=0x{b:04x}")
    if name == "WQ_PEND2":
        return f"WQ_PEND2 pended_on_high16=0x{b:04x} (combine -> addr, resolve in zmk.elf)"
    if name == "SLOT":
        st = (b >> 8) & 0xFF
        wf = b & 0xFF
        stname = STATUS_NAMES.get(st, f"status=0x{st:02x}")
        wfbits = [n for m, n in [(1, "QUEUED"), (2, "RUNNING"),
                                 (4, "CANCELING"), (16, "DELAYED")] if wf & m]
        return (f"SLOT ep=0x{a:02x} {stname} work={'|'.join(wfbits) or 'idle'}")
    if name == "UNCONF":
        return f"UNCONF attached-but-unconfigured {b} ticks -> recovery"
    if name == "WSTALL":
        return (f"WSTALL slot {a}: k_work QUEUED {b} ticks ({b // 2}s) without "
                f"running -> recovery")
    if name == "WDETAIL":
        bits = [n for m, n in [(1, "RUNNING"), (2, "CANCELING"), (4, "QUEUED"),
                               (8, "DELAYED"), (16, "FLUSHING")] if b & m]
        return f"WDETAIL slot {a}: raw flags=0x{b:04x} ({'|'.join(bits) or 'clear'})"
    if name == "WQPTR":
        on_usb_wq = bool(b & 1)
        in_list = bool(b & 2)
        has_q = bool(b & 4)
        verdict = ("consistent (queued and on the list)" if in_list
                   else "FLAG/LIST DIVERGENCE — queued flag set, node not on the "
                        "list: this item can never run" if on_usb_wq
                   else "not on the USB workqueue")
        return (f"WQPTR slot {a}: queue={'usb_wq' if on_usb_wq else ('other' if has_q else 'NULL')} "
                f"in_pending={in_list} -> {verdict}")
    if name == "WQ_QSTAT":
        # k_work_busy_get bits: 1=QUEUED 2=RUNNING 4=CANCELING (0x10=DELAYED)
        flags = [n for m, n in [(1, "QUEUED"), (2, "RUNNING"), (4, "CANCELING"),
                                (16, "DELAYED")] if a & m]
        verdict = ("ORPHANED items (flagged queued, list empty)" if (a & 1) and not (b & 1)
                   else "LOST WAKEUP (items in list, thread idle)" if b & 1
                   else "submits failing (marker not queued)")
        return (f"WQ_QSTAT marker={'|'.join(flags) or 'idle'} "
                f"queue_{'non' if b & 1 else ''}empty -> {verdict}")
    return f"{name} a={a} b={b}"


def main():
    args = [a for a in sys.argv[1:]]
    saved = "--saved" in args
    if saved:
        args.remove("--saved")
    port = args[0] if args else None
    kb = rgb.Rainy75(port)
    # _request() frames with the module-global group id; retarget it at
    # flash_mgmt (group 64) instead of rgb_mgmt (65).
    rgb.RGB_GROUP = FLASH_GROUP

    events = []
    seq = 0
    off = 0
    while True:
        payload = [("off", rgb._cbor_uint(off))]
        if saved:
            payload.append(("saved", rgb._cbor_uint(1)))
        body = kb._request(rgb.SMP_OP_READ, CMD_USB_DIAG, payload)
        seq = body.get("seq", 0)
        n = body.get("n", 0)
        data = body.get("data", b"")
        for i in range(n):
            ms, code, a, b = struct.unpack_from("<IBBH", data, i * 8)
            events.append((ms, code, a, b))
        off += n
        if n < PAGE:
            break
    kb.close()

    which = "saved (persisted at last starvation)" if saved else "live"
    print(f"usb_diag [{which}]: {len(events)} events (total ever: {seq})")
    if saved and not events:
        print("  (no saved ring — the watchdog has not fired since flashing)")
    prev_ms = None
    for ms, code, a, b in events:
        t = f"{ms // 3600000:3d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}"
        delta = "" if prev_ms is None else f" (+{(ms - prev_ms) / 1000:.1f}s)"
        prev_ms = ms
        print(f"  [{t}.{ms % 1000:03d}] {describe(code, a, b)}{delta}")


if __name__ == "__main__":
    main()
