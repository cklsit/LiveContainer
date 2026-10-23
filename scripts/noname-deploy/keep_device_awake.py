#!/usr/bin/env python3
"""Renew a PreventUserIdleSystemSleep assertion for the paired iPad."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from discover_ios_devices import discover
from launch_ios_app import hold_awake


async def run(interval: int, assertion_timeout: int) -> None:
    while True:
        try:
            discovered = await discover(udid=None, timeout=4)
        except Exception as error:
            print(f"Discovery failed; retrying: {error}", flush=True)
            await asyncio.sleep(interval)
            continue
        devices = [
            device
            for device in discovered
            if device.get("deviceClass") == "iPad" or str(device.get("productType", "")).startswith("iPad")
        ]
        if len(devices) == 1:
            device_id = str(devices[0]["identifier"])
            try:
                await hold_awake(device_id, assertion_timeout)
                print(f"Awake assertion renewed for {device_id}", flush=True)
            except Exception as error:
                print(f"Could not renew awake assertion: {error}", flush=True)
        elif len(devices) > 1:
            print("Skipped awake assertion because multiple iPads are visible", flush=True)
        await asyncio.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--assertion-timeout", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asyncio.run(run(args.interval, args.assertion_timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
