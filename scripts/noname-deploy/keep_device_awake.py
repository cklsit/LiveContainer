#!/usr/bin/env python3
"""Renew a PreventUserIdleSystemSleep assertion for the paired iPad.

Each ``hold_awake()`` call holds the assertion for a bounded window and blocks
for that window, so this loop renews it right after it returns:

* ``--assertion-timeout`` is the length of one assertion window (default 120s).
* ``--interval`` is only the minimum gap between two renewals (default 30s).

Keep ``--interval`` well below ``--assertion-timeout`` so the device never idles
long enough to sleep between renewals. Discovery is repeated on every cycle and
the assertion is only held while exactly one iPad is visible.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from discover_ios_devices import discover
from launch_ios_app import MAX_AWAKE_HOLD_SECONDS, hold_awake


async def sleep_for_interval(interval: int, started_at: float) -> None:
    """Sleep only the remainder of the minimum interval between renewals."""
    remaining = interval - (time.monotonic() - started_at)
    if remaining > 0:
        await asyncio.sleep(remaining)


async def run(interval: int, assertion_timeout: int) -> None:
    interval = max(1, int(interval))
    assertion_timeout = min(MAX_AWAKE_HOLD_SECONDS, max(interval + 1, int(assertion_timeout)))
    print(
        f"Renewing the awake assertion every {assertion_timeout}s "
        f"with a {interval}s minimum gap",
        flush=True,
    )

    while True:
        cycle_started = time.monotonic()
        try:
            discovered = await discover(udid=None, timeout=4)
        except Exception as error:
            print(f"Discovery failed; retrying in {interval}s: {error}", flush=True)
            await sleep_for_interval(interval, cycle_started)
            continue

        devices = [
            device
            for device in discovered
            if device.get("deviceClass") == "iPad"
            or str(device.get("productType", "")).startswith("iPad")
        ]
        if len(devices) == 1:
            device_id = str(devices[0]["identifier"])
            renewed_at = time.monotonic()
            try:
                # Blocks for assertion_timeout while the assertion is held.
                await hold_awake(device_id, assertion_timeout)
                print(
                    f"Awake assertion held for {assertion_timeout}s on {device_id} "
                    f"(elapsed {time.monotonic() - renewed_at:.1f}s); renewing",
                    flush=True,
                )
            except Exception as error:
                print(f"Could not renew awake assertion: {error}", flush=True)
            await sleep_for_interval(interval, renewed_at)
        elif len(devices) > 1:
            print("Skipped awake assertion because multiple iPads are visible", flush=True)
            await sleep_for_interval(interval, cycle_started)
        else:
            print("No iPad is visible; the awake assertion is not held", flush=True)
            await sleep_for_interval(interval, cycle_started)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Minimum gap in seconds between two assertion renewals",
    )
    parser.add_argument(
        "--assertion-timeout",
        type=int,
        default=120,
        help=(
            "Length of one assertion window in seconds (capped at "
            f"{MAX_AWAKE_HOLD_SECONDS}); keep it short so a dropped tunnel only "
            "leaves a short gap"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args.interval, args.assertion_timeout))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
