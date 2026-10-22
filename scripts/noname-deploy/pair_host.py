#!/usr/bin/env python3
"""Create a one-time, device-initiated RemotePairing trust for the LAN runner."""

from __future__ import annotations

import argparse
import asyncio
import json

from pymobiledevice3.remote.tunnel_service import PairableHostInfo, serve_pairable_host


async def pair(name: str, model: str, timeout: float) -> None:
    host = PairableHostInfo(name=name, model=model)
    print(f'Advertising "{name}" with identifier {host.identifier}', flush=True)
    print(
        "On the iPad, open Settings > Developer > Paired Macs, choose this host "
        "under Other Devices, and enter the displayed code.",
        flush=True,
    )

    def show_pin(pin: str) -> None:
        print(f"\nPairing code: {pin}\n", flush=True)

    def show_wait(elapsed: float) -> None:
        print(f"Waiting for the iPad pairing screen ({int(elapsed)}s)", flush=True)

    result = await serve_pairable_host(
        host,
        pin_callback=show_pin,
        waiting_callback=show_wait,
        timeout=timeout,
    )
    print(
        json.dumps(
            {
                "name": result.peer_device.name,
                "model": result.peer_device.model,
                "udid": result.peer_device.udid,
                "record": str(result.record_path),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Synology-Noname")
    parser.add_argument("--model", default="Mac17,7")
    parser.add_argument("--timeout", type=float, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(pair(args.name, args.model, args.timeout))
    except TimeoutError:
        print("error: no iPad started pairing before the timeout", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
