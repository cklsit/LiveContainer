#!/usr/bin/env python3
"""Launch a paired iPad app through an in-process RemoteXPC tunnel."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from typing import Any

from pymobiledevice3.lockdown import get_mobdev2_lockdowns
from pymobiledevice3.remote import tunnel_service, userspace_tunnel
from pymobiledevice3.remote.core_device.app_service import AppServiceService


class LaunchError(RuntimeError):
    pass


async def create_mobdev2_tunnel_provider(serial: str | None, _autopair: bool, _fallback: bool):
    if not serial:
        raise LaunchError("An explicit device UDID is required")

    async for _ip, lockdown in get_mobdev2_lockdowns(udid=serial, only_paired=True):
        try:
            provider = await tunnel_service.CoreDeviceTunnelProxy.create(lockdown)
        except Exception:
            await lockdown.close()
            break
        return provider, lockdown

    services = await tunnel_service.get_remote_pairing_tunnel_services(udid=serial, bonjour_timeout=8)
    if not services:
        raise LaunchError(
            f"No RemotePairing tunnel is available for {serial}. "
            "Complete the one-time `pymobiledevice3 remote pair-host` setup."
        )
    return services[0], None


async def launch(udid: str, bundle_id: str, arguments: list[str]) -> dict[str, Any]:
    # pymobiledevice3 selects its no-root tunnel through usbmux first. The runner
    # has no local usbmuxd, so feed the already-authenticated mobdev2 lockdown
    # client directly into its userspace RemoteXPC implementation instead.
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        async with AppServiceService(rsd) as app_service:
            result = await app_service.launch_application(
                bundle_id,
                arguments=arguments,
                kill_existing=True,
            )
            return {"ok": True, "result": result}
    finally:
        await tunnel.aclose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--token-env")
    parser.add_argument("argument", nargs="*")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.token_env:
        token = os.environ.get(args.token_env, "")
        if len(token) < 32:
            print(f"error: {args.token_env} must contain a deployment token", file=sys.stderr)
            return 1
        args.argument.append(f"noname-deploy-token={token}")
    try:
        result = asyncio.run(launch(args.udid, args.bundle_id, args.argument))
    except Exception as error:
        if os.environ.get("NONAME_DEPLOY_DEBUG") == "1":
            traceback.print_exc()
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
