"""Synchronized motion of 4 ADrive actuators (USB and CAN mixed).

    python sync_4axis.py                                   # every USB actuator found
    python sync_4axis.py --extra can:pcan:usb1:16 can:pcan:usb1:17  # + CAN nodes (explicit)
    python sync_4axis.py --sim 3                           # + 3 simulated axes (no hardware)

What it does
1. adrive.connect_all(): opens every actuator found plus the explicit extra targets.
2. Homes all axes at the same time (one thread each) — required after power-up.
3. Runs coordinated back-and-forth moves on a common time base: every axis gets
   its own distance but the same move duration and profile shape, so all axes
   start and arrive together.
4. Prints, per move: start sync error (spread of command times and of actual
   motion onsets), axis-to-axis phase error (spread of the times each axis passes
   its midpoint), and each axis' worst deviation from its ideal profile.
5. If any axis reports an error, every axis is stopped at once.

CAN 경로 / CAN
    IMPORTANT — with the current firmware CAN axes cannot move. The SDK refuses
    homing over CAN (not supported by the current firmware) and refuses motion
    on an axis it has not homed. A CAN axis in the mix therefore makes step 2
    fail, and the example stops every axis and exits. CAN axes can be
    discovered, monitored and stopped only, until a later firmware release
    enables CAN motion. Use USB for motion until then. Real CAN adapters are untested on
    hardware in this release.

    Targets: can:<backend>:<channel>:<node id>, passed with --extra, e.g.
        python sync_4axis.py --extra can:pcan:usb1:16 can:pcan:usb1:17
        python sync_4axis.py --extra can:slcan:COM5:16
        python sync_4axis.py --extra can:gsusb:0:16
        python sync_4axis.py --extra can:socketcan:can0:16
    Or rely on auto-detect: the SDK listens for drive heartbeats (~1.6 s) and
    finds axes by CAN node id (default 16 = 0x10). Serial number and firmware
    are not available over CAN. Set ADRIVE_CAN_SCAN=0 to skip CAN probing.

    Wiring: all CAN axes on one bus, each with its own node id (duplicate ids
    are left out of auto-detect with a warning). 120 ohm termination at both ends of the
    bus. Bitrate 1 Mbit/s.

    Adapters:
      pcan       PEAK PCAN-USB. Install PEAK's PCAN-Basic driver (DLL) first.
      gsusb      candleLight / gs_usb (e.g. CANable with candleLight firmware).
                 Needs the WinUSB driver.
      slcan      CANable stock firmware. Appears as a COM port (COM5 above).
      socketcan  Linux. Bring the interface up at 1 Mbit/s beforehand.
"""
import argparse
import math
import threading
import time

import adrive

ACCEL_FRACTION = 0.25   # share of the move spent accelerating (and again decelerating)


def home_all(acts):
    errors = {}

    def run(name, act):
        try:
            act.home(timeout=60.0)
        except adrive.ADriveError as e:
            errors[name] = e
            for other in acts.values():   # one axis failed: abort every axis' homing too
                try:
                    other.estop()
                except adrive.ADriveError:
                    pass

    threads = [threading.Thread(target=run, args=item) for item in acts.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise RuntimeError(f"homing failed: {errors}")


def profile(d, T):
    """Speed and accel so that a move of length d takes exactly T seconds."""
    v = d / (T * (1 - ACCEL_FRACTION))
    return v, v / (ACCEL_FRACTION * T)


def ideal(d, T, t):
    """Distance covered at time t on that profile."""
    ta, v = ACCEL_FRACTION * T, d / (T * (1 - ACCEL_FRACTION))
    a = v / ta
    t = min(max(t, 0.0), T)
    if t < ta:
        return 0.5 * a * t * t
    if t < T - ta:
        return 0.5 * a * ta * ta + v * (t - ta)
    r = T - t
    return d - 0.5 * a * r * r


def coordinated_move(acts, targets, max_speed, max_accel):
    starts = {n: a.position() for n, a in acts.items()}
    dist = {n: abs(targets[n] - starts[n]) for n in acts}
    f = ACCEL_FRACTION
    # common duration: the slowest axis must stay within the speed/accel limits
    T = max(max(d / (max_speed * (1 - f)), math.sqrt(d / (max_accel * f * (1 - f)))) for d in dist.values())
    sent = {}
    t0 = time.monotonic()
    for n, a in acts.items():
        v, acc = profile(max(dist[n], 1e-6), T)
        sent[n] = time.monotonic() - t0
        a.move_to(targets[n], speed=v, accel=acc, wait=False)
    issued = time.monotonic() - t0       # all commands out: first-to-last = start sync error on our side
    onset, mid, dev = {}, {}, {n: 0.0 for n in acts}
    while time.monotonic() - t0 < T + 0.5:
        for n, a in acts.items():
            p = a.position()                      # raises if this axis faults
            t = time.monotonic() - t0
            moved = abs(p - starts[n])
            if n not in onset and moved > 0.005:
                onset[n] = t
            if n not in mid and moved >= dist[n] / 2:
                mid[n] = t
            dev[n] = max(dev[n], abs(moved - ideal(dist[n], T, t - sent[n])))

    def spread(x):
        return (max(x.values()) - min(x.values())) * 1000 if len(x) == len(acts) else float("nan")

    return {"T": T, "cmd_ms": issued * 1000, "onset_ms": spread(onset), "phase_ms": spread(mid), "dev": dev,
            "final": {n: acts[n].position() - targets[n] for n in acts}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extra", nargs="*", default=[], help="explicit targets, e.g. can:pcan:usb1:16 (see CAN above)")
    ap.add_argument("--sim", type=int, default=0, help="add N simulated axes")
    ap.add_argument("--low", type=float, default=3.0, help="mm")
    ap.add_argument("--high", type=float, default=13.0, help="mm")
    ap.add_argument("--speed", type=float, default=20.0, help="max axis speed, mm/s")
    ap.add_argument("--accel", type=float, default=100.0, help="max axis accel, mm/s^2")
    ap.add_argument("--cycles", type=int, default=3)
    args = ap.parse_args()

    acts = adrive.connect_all(args.extra + [f"sim:{k + 1}" for k in range(args.sim)])
    for n, a in acts.items():
        print(f"{n:22s} {a.info()}")
    try:
        home_all(acts)
        for a in acts.values():
            a.enable()
        # each axis its own stroke (100 %, 85 %, 70 %, ...) to show that they still stay in phase
        span = args.high - args.low
        top = {n: args.low + span * (1 - 0.15 * k) for k, n in enumerate(acts)}
        bottom = {n: args.low for n in acts}
        coordinated_move(acts, bottom, args.speed, args.accel)
        print(f"{'move':>6} {'T[s]':>6} {'cmd skew[ms]':>13} {'onset skew[ms]':>15} {'phase err[ms]':>14}  worst profile deviation per axis [mm]")
        for k in range(args.cycles * 2):
            r = coordinated_move(acts, top if k % 2 == 0 else bottom, args.speed, args.accel)
            devs = " ".join(f"{d:.3f}" for d in r["dev"].values())
            print(f"{k + 1:>6} {r['T']:6.2f} {r['cmd_ms']:13.1f} {r['onset_ms']:15.1f} {r['phase_ms']:14.1f}  {devs}")
        print("final error per axis [um]:", {n: round(e * 1000, 1) for n, e in r["final"].items()})
    except BaseException as e:
        for a in acts.values():           # one axis failed (or Ctrl-C): stop every axis
            try:
                a.estop()
            except adrive.ADriveError:
                pass
        print(f"STOPPED ALL AXES: {type(e).__name__}: {e}")
        raise
    finally:
        for a in acts.values():
            a.close()


if __name__ == "__main__":
    main()
