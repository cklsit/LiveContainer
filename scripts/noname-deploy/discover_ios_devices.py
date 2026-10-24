#!/usr/bin/env python3
"""Discover paired iPads over mobdev2 or RemotePairing without the pmd3 CLI."""

from __future__ import annotations

import argparse
import asyncio
import json

from pymobiledevice3.lockdown import get_mobdev2_lockdowns
from pymobiledevice3.remote import tunnel_service


async def discover(udid: str | None, timeout: float) -> list[dict[str, str]]:
    devices: dict[str, dict[str, str]] = {}

    try:
        async for ip, lockdown in get_mobdev2_lockdowns(
            udid=udid,
            only_paired=True,
            timeout=timeout,
        ):
            try:
                info = lockdown.short_info
                identifier = str(info.get("UniqueDeviceID") or lockdown.udid or "")
                if identifier:
                    devices[identifier] = {
                        "identifier": identifier,
                        "name": str(info.get("DeviceName") or info.get("ProductType") or identifier),
                        "deviceClass": str(info.get("DeviceClass") or ""),
                        "productType": str(info.get("ProductType") or ""),
                        "host": ip,
                        "transport": "mobdev2",
                    }
            finally:
                await lockdown.close()
    except Exception:
        pass

    try:
        services = await tunnel_service.get_remote_pairing_tunnel_services(
            udid=udid,
            bonjour_timeout=timeout,
        )
        for service in services:
            try:
                identifier = str(service.remote_identifier)
                devices.setdefault(
                    identifier,
                    {
                        "identifier": identifier,
                        "name": f"RemotePairing iPad {identifier[-8:]}",
                        "deviceClass": "iPad",
                        "productType": "",
                        "host": str(service.hostname),
                        "transport": "remotepairing",
                    },
                )
            finally:
                await service.close()
    except Exception:
        pass

    return list(devices.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid")
    parser.add_argument("--timeout", type=float, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    devices = asyncio.run(discover(args.udid, args.timeout))
    print(json.dumps({"devices": devices}, separators=(",", ":")))
    return 0 if devices else 1


if __name__ == "__main__":
    raise SystemExit(main())
