#!/usr/bin/env python3
"""Install an IPA directly over the trusted RemoteXPC tunnel."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from launch_ios_app import create_mobdev2_tunnel_provider
from pymobiledevice3.remote import userspace_tunnel
from pymobiledevice3.services.installation_proxy import InstallationProxyService


async def install(udid: str, ipa_path: Path, developer: bool) -> dict[str, object]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        async with InstallationProxyService(rsd) as service:
            await service.install_from_local(ipa_path, developer=developer)
        return {"ok": True, "ipa": str(ipa_path), "developer": developer}
    finally:
        await tunnel.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True)
    parser.add_argument("--ipa", required=True, type=Path)
    parser.add_argument("--developer", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(install(args.udid, args.ipa.resolve(), args.developer))
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
