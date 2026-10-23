#!/usr/bin/env python3
"""Launch a paired iPad app through an in-process RemoteXPC tunnel."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import sys
import traceback
from pathlib import Path
from typing import Any

from pymobiledevice3.lockdown import get_mobdev2_lockdowns
from pymobiledevice3.remote import tunnel_service, userspace_tunnel
from pymobiledevice3.remote.core_device.app_service import AppServiceService
from pymobiledevice3.remote.core_device.hid_service import (
    TOUCHSCREEN_STATE_CONTACT,
    TOUCHSCREEN_STATE_RELEASE,
    touch_session,
)
from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit


class LaunchError(RuntimeError):
    pass


async def installed_bundle_ids(app_service: AppServiceService) -> list[str]:
    apps = await app_service.list_apps()
    return sorted(
        {
            str(app.get("bundleIdentifier"))
            for app in apps
            if isinstance(app, dict) and app.get("bundleIdentifier")
        }
    )


def resolve_bundle_id(requested: str, installed: list[str]) -> str:
    if requested in installed:
        return requested
    candidates = [identifier for identifier in installed if identifier.startswith(f"{requested}.")]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise LaunchError(f"Installed app was not found for {requested}")
    raise LaunchError(f"Multiple installed apps match {requested}: {', '.join(candidates)}")


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
            resolved_bundle_id = resolve_bundle_id(
                bundle_id,
                await installed_bundle_ids(app_service),
            )
            result = await app_service.launch_application(
                resolved_bundle_id,
                arguments=arguments,
                kill_existing=True,
            )
            return {"ok": True, "bundleID": resolved_bundle_id, "result": result}
    finally:
        await tunnel.aclose()


async def open_url(udid: str, bundle_id: str, payload_url: str) -> dict[str, Any]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        async with AppServiceService(rsd) as app_service:
            resolved_bundle_id = resolve_bundle_id(
                bundle_id,
                await installed_bundle_ids(app_service),
            )
            result = await app_service.invoke(
                "com.apple.coredevice.feature.launchapplication",
                {
                    "applicationSpecifier": {
                        "bundleIdentifier": {"_0": resolved_bundle_id},
                    },
                    "options": {
                        "arguments": [],
                        "environmentVariables": {},
                        "standardIOUsesPseudoterminals": True,
                        "startStopped": False,
                        "terminateExisting": True,
                        "user": {"shortName": "mobile"},
                        "platformSpecificOptions": plistlib.dumps({}),
                        "payloadURL": {"relative": payload_url},
                        "activates": True,
                    },
                    "standardIOIdentifiers": {},
                },
            )
            return {"ok": True, "bundleID": resolved_bundle_id, "payloadURL": payload_url, "result": result}
    finally:
        await tunnel.aclose()


async def list_apps(udid: str) -> dict[str, Any]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        async with AppServiceService(rsd) as app_service:
            return {"ok": True, "bundleIdentifiers": await installed_bundle_ids(app_service)}
    finally:
        await tunnel.aclose()


async def control_device(
    udid: str,
    *,
    screenshot: Path | None,
    tap: tuple[int, int] | None,
) -> dict[str, Any]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        result: dict[str, Any] = {"ok": True}
        if screenshot is not None:
            async with ScreenCaptureService(rsd) as service:
                response = await service.capture_screenshot()
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            screenshot.write_bytes(response["image"])
            result["screenshot"] = str(screenshot)
        if tap is not None:
            x, y = tap
            async with touch_session(rsd) as service:
                await service.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y)
                await asyncio.sleep(0.05)
                await service.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x, y)
            result["tap"] = [x, y]
        return result
    finally:
        await tunnel.aclose()


async def accessibility_action(
    udid: str,
    *,
    list_elements: bool,
    press_label: str | None,
) -> dict[str, Any]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        elements: list[dict[str, Any]] = []
        pressed = False
        async with AccessibilityAudit(rsd) as accessibility:
            async for focused in accessibility.iter_elements():
                item = focused.to_dict()
                elements.append(item)
                if press_label and (item.get("caption") or item.get("spoken_description")) == press_label:
                    element = focused.element
                    if element is None:
                        raise LaunchError(f"Accessibility element {press_label!r} has no platform identifier")
                    await accessibility.perform_press(element.identifier)
                    pressed = True
                    break
                if not list_elements and press_label is None:
                    break
        return {"ok": True, "elements": elements, "pressed": pressed}
    finally:
        await tunnel.aclose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True)
    parser.add_argument("--bundle-id")
    parser.add_argument("--token-env")
    parser.add_argument("--list-apps", action="store_true")
    parser.add_argument("--open-url")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--tap", nargs=2, type=int, metavar=("X", "Y"))
    parser.add_argument("--accessibility-list", action="store_true")
    parser.add_argument("--accessibility-press")
    parser.add_argument("argument", nargs="*")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (
        not args.list_apps
        and args.screenshot is None
        and args.tap is None
        and not args.accessibility_list
        and args.accessibility_press is None
        and not args.bundle_id
    ):
        print("error: --bundle-id is required unless --list-apps is used", file=sys.stderr)
        return 1
    if args.token_env:
        token = os.environ.get(args.token_env, "")
        if len(token) < 32:
            print(f"error: {args.token_env} must contain a deployment token", file=sys.stderr)
            return 1
        args.argument.append(f"noname-deploy-token={token}")
    try:
        if args.list_apps:
            result = asyncio.run(list_apps(args.udid))
        elif args.open_url:
            assert args.bundle_id is not None
            result = asyncio.run(open_url(args.udid, args.bundle_id, args.open_url))
        elif args.screenshot is not None or args.tap is not None:
            tap = tuple(args.tap) if args.tap is not None else None
            result = asyncio.run(
                control_device(
                    args.udid,
                    screenshot=args.screenshot,
                    tap=tap,
                )
            )
        elif args.accessibility_list or args.accessibility_press:
            result = asyncio.run(
                accessibility_action(
                    args.udid,
                    list_elements=args.accessibility_list,
                    press_label=args.accessibility_press,
                )
            )
        else:
            assert args.bundle_id is not None
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
