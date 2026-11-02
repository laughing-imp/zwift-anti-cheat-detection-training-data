#!/usr/bin/env python3
"""List nearby Bluetooth devices and flag any that advertise Cycling Power.

Run this on the machine running Zwift to check whether fake_power_meter.py
(or any power meter) can be seen. Needs `bleak` (pip install bleak).

Usage:
  python3 scan_power_meters.py [--seconds 10]
"""
import argparse
import asyncio

from bleak import BleakScanner

CPS = "00001818-0000-1000-8000-00805f9b34fb"


async def main(seconds):
    print(f"Scanning for {seconds} s...")
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = sorted(found.values(), key=lambda da: -(da[1].rssi or -999))
    for device, adv in rows:
        name = adv.local_name or device.name or "(no name)"
        cps = CPS in [u.lower() for u in adv.service_uuids]
        mark = "POWER METER" if cps else ""
        print(f"{adv.rssi:5d} dBm  {name:24.24s}  {mark}")
    if not any(CPS in [u.lower() for u in a.service_uuids] for _, a in rows):
        print("\nNo device is advertising the Cycling Power service.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=10)
    asyncio.run(main(p.parse_args().seconds))
