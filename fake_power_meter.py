#!/usr/bin/env python3
"""Make this Mac act as a Bluetooth (BLE) power meter.

Advertises the standard Cycling Power Service (0x1818), so Zwift or any other
app on another machine can pair it as a "Power Source" and "Cadence" sensor.
It sends a steady power value (default 110 W) with small random jitter, plus
crank revolution data so the receiving app can work out cadence.

The device name defaults to "claudecode" (close to the "claude code" label
make_variant_fit.py uses), so rides recorded from it are easy to tell apart in
training data. Keep names to 10 characters or less: BLE advertisements are
small, and a longer name may be cut off.

Needs the `bless` package (pip install bless). On first run macOS will ask to
let your terminal use Bluetooth; allow it.

Usage:
  python3 fake_power_meter.py [--watts 110] [--jitter 3] [--cadence 85]
      [--name claudecode] [--hz 1]
"""
import argparse
import asyncio
import random
import struct
import time

from bless import (
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
)


def uuid16(short):
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


CPS_SERVICE = uuid16(0x1818)          # Cycling Power Service
CP_MEASUREMENT = uuid16(0x2A63)       # notify
CP_FEATURE = uuid16(0x2A65)           # read
SENSOR_LOCATION = uuid16(0x2A5D)      # read

FEATURE_CRANK_REVS = 0x08             # Cycling Power Feature bit 3
FLAG_CRANK_REVS = 0x20                # Measurement flags bit 5
LOCATION_REAR_HUB = 12


class Crank:
    """Tracks cumulative crank revolutions and last event time (1/1024 s)."""

    def __init__(self):
        self.start = time.monotonic()
        self.revs = 0
        self.last_event = 0.0         # seconds since start

    def update(self, cadence_rpm):
        now = time.monotonic() - self.start
        if cadence_rpm > 0:
            period = 60.0 / cadence_rpm
            while self.last_event + period <= now:
                self.last_event += period
                self.revs += 1
        return self.revs & 0xFFFF, int(self.last_event * 1024) & 0xFFFF


def measurement(watts, revs, event_time):
    # flags (uint16), instantaneous power (sint16),
    # cumulative crank revs (uint16), last crank event time (uint16)
    return bytearray(struct.pack("<HhHH", FLAG_CRANK_REVS, watts, revs, event_time))


async def run(args):
    server = BlessServer(name=args.name)
    read = GATTCharacteristicProperties.read
    notify = GATTCharacteristicProperties.notify
    readable = GATTAttributePermissions.readable

    await server.add_new_service(CPS_SERVICE)
    await server.add_new_characteristic(
        CPS_SERVICE, CP_MEASUREMENT, notify, None, readable)
    await server.add_new_characteristic(
        CPS_SERVICE, CP_FEATURE, read,
        bytearray(struct.pack("<I", FEATURE_CRANK_REVS)), readable)
    await server.add_new_characteristic(
        CPS_SERVICE, SENSOR_LOCATION, read,
        bytearray([LOCATION_REAR_HUB]), readable)

    # Always advertise the Cycling Power UUID: Zwift only lists devices that
    # advertise it, and bless drops it for names over 10 characters by default.
    await server.start(prioritize_local_name=False)
    print(f'Advertising power meter "{args.name}" '
          f"at {args.watts} W (+/-{args.jitter}), {args.cadence} rpm. Ctrl-C to stop.")

    crank = Crank()
    interval = 1.0 / args.hz
    try:
        while True:
            watts = max(0, args.watts + random.randint(-args.jitter, args.jitter))
            cadence = max(0, args.cadence + random.uniform(-2, 2)) if args.cadence else 0
            revs, event_time = crank.update(cadence)
            server.get_characteristic(CP_MEASUREMENT).value = measurement(
                watts, revs, event_time)
            server.update_value(CPS_SERVICE, CP_MEASUREMENT)
            print(f"\r{watts:4d} W  {cadence:5.1f} rpm  revs={revs}", end="", flush=True)
            await asyncio.sleep(interval)
    finally:
        await server.stop()


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--watts", type=int, default=110, help="target power (W)")
    p.add_argument("--jitter", type=int, default=3, help="random +/- watts per sample")
    p.add_argument("--cadence", type=float, default=85, help="cadence (rpm), 0 to disable")
    p.add_argument("--name", default="claudecode",
                   help="advertised device name (10 characters max)")
    p.add_argument("--hz", type=float, default=1, help="updates per second")
    args = p.parse_args()
    if len(args.name) > 10:
        p.error("--name must be 10 characters or less to fit in the advertisement")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
