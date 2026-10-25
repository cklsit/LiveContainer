#!/usr/bin/env python3
"""Launch a paired iPad app through an in-process RemoteXPC tunnel.

``--hold-awake SECONDS`` holds a ``PreventUserIdleSystemSleep`` power assertion
for the whole window and therefore blocks the process for SECONDS before it
prints its JSON result. Wrap it in a background process when the caller needs to
keep working; ``keep_device_awake.py`` renews the assertion that way.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import plistlib
import re
import sys
import traceback
import urllib.parse
from pathlib import Path
from typing import Any

from pymobiledevice3.lockdown import get_mobdev2_lockdowns
from pymobiledevice3.remote import tunnel_service, userspace_tunnel
from pymobiledevice3.remote.core_device.app_service import AppServiceService
from pymobiledevice3.remote.core_device.file_service import Domain, FileServiceService
from pymobiledevice3.remote.core_device.hid_service import (
    HID_BUTTON_STATE_DOWN,
    HID_BUTTON_STATE_UP,
    TOUCHSCREEN_STATE_CONTACT,
    TOUCHSCREEN_STATE_RELEASE,
    IndigoHIDService,
    touch_session,
)
from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.power_assertion import PowerAssertionService

from passcode import passcode_usages


class LaunchError(RuntimeError):
    pass


# Upper bound for one power-assertion window. Longer windows leave the RemoteXPC
# tunnel idle for too long, and callers that need continuous coverage renew a
# short window instead.
MAX_AWAKE_HOLD_SECONDS = 300

# The lock screen reports localized accessibility captions ("已锁定" on a Chinese
# iPad). Matching only the English word made the old code believe a locked device
# was already unlocked, so the passcode path was never reached reliably.
LOCKED_MARKERS = ("已锁定", "已锁屏", "locked")
PASSCODE_PROMPT_MARKERS = ("使用密码", "use passcode", "输入密码", "enter passcode")
PASSCODE_KEYPAD_MARKERS = ("已输入", "password field", "passcode field")

# "使用密码" in the Touch ID alert of a Home-button iPad, measured in landscape.
USE_PASSCODE_TAP = (29556, 33030)

# Numeric keypad centers in landscape, normalized to the 0-65535 HID space.
PASSCODE_KEY_TAPS = {
    "1": (27260, 25630),
    "2": (33100, 25630),
    "3": (39060, 25630),
    "4": (27260, 34010),
    "5": (33100, 34010),
    "6": (39060, 34010),
    "7": (27260, 42470),
    "8": (33100, 42470),
    "9": (39060, 42470),
    "0": (33100, 50860),
}


def captions_report_locked(captions: list[str]) -> bool:
    return any(marker in caption.casefold() for caption in captions for marker in LOCKED_MARKERS)


def captions_show_passcode_keypad(captions: list[str]) -> bool:
    return any(marker in caption.casefold() for caption in captions for marker in PASSCODE_KEYPAD_MARKERS)


def captions_show_passcode_prompt(captions: list[str]) -> bool:
    return any(marker in caption.casefold() for caption in captions for marker in PASSCODE_PROMPT_MARKERS)


async def read_passcode_digit_count(rsd: Any) -> int | None:
    return passcode_digit_count(await accessibility_captions(rsd))


def passcode_digit_count(captions: list[str]) -> int | None:
    """Read "已输入N位" / "N digits entered" from the keypad accessibility tree."""
    for caption in captions:
        match = re.search(r"已输入\s*(\d+)\s*位", caption)
        if match:
            return int(match.group(1))
        match = re.search(r"(\d+)\s+digits?\s+entered", caption, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


async def press_home(rsd: Any) -> None:
    async with IndigoHIDService(rsd) as service:
        await service.send_button(0x0C, 0x40, HID_BUTTON_STATE_DOWN)
        await asyncio.sleep(0.05)
        await service.send_button(0x0C, 0x40, HID_BUTTON_STATE_UP)


async def tap(rsd: Any, point: tuple[int, int]) -> None:
    x, y = point
    async with touch_session(rsd) as service:
        await service.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y)
        await asyncio.sleep(0.08)
        await service.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x, y)


async def open_passcode_keypad(rsd: Any) -> bool:
    """Bring the numeric passcode keypad to the front on a locked iPad.

    A Home-button iPad shows the Touch ID alert first ("使用密码" / "取消"); the
    keypad only appears after tapping "使用密码". Face ID iPads show the keypad
    right after a wake, so the loop also accepts that state directly.
    """
    for attempt in range(3):
        captions = await accessibility_captions(rsd)
        if captions_show_passcode_keypad(captions):
            return True
        if captions_show_passcode_prompt(captions):
            await tap(rsd, USE_PASSCODE_TAP)
            await asyncio.sleep(1.5)
            if captions_show_passcode_keypad(await accessibility_captions(rsd)):
                return True
            # The tap missed or dismissed the dialog; bring it back below.

        # A single Home press only wakes the display on a Home-button iPad.
        # The Touch ID / "使用密码" dialog appears after a second, separate
        # press, and swiping up dismisses it again. Use two presses with a
        # gap long enough that iOS does not read them as a double-click.
        await press_home(rsd)
        await asyncio.sleep(1.2)
        await press_home(rsd)
        await asyncio.sleep(1.5)
        captions = await accessibility_captions(rsd)
        print(
            f"[unlock] after two home presses ({attempt + 1}/3): {captions[:3]}",
            file=sys.stderr,
            flush=True,
        )
        if captions_show_passcode_keypad(captions):
            return True
        if captions_show_passcode_prompt(captions):
            await tap(rsd, USE_PASSCODE_TAP)
            await asyncio.sleep(1.5)
            if captions_show_passcode_keypad(await accessibility_captions(rsd)):
                return True
    return False


def screenshot_is_portrait(image: bytes) -> bool:
    """Return True when a PNG screenshot is taller than it is wide."""
    if len(image) < 24 or image[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    width = int.from_bytes(image[16:20], "big")
    height = int.from_bytes(image[20:24], "big")
    return height > width


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


async def open_url(
    udid: str,
    bundle_id: str,
    payload_url: str,
    arguments: list[str],
) -> dict[str, Any]:
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
                        "arguments": arguments,
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


async def app_info(udid: str) -> dict[str, Any]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        async with InstallationProxyService(rsd) as service:
            apps = await service.lookup({
                "ApplicationType": "User",
                "ReturnAttributes": [
                    "CFBundleIdentifier",
                    "CFBundleShortVersionString",
                    "CFBundleVersion",
                    "Path",
                    "Container",
                ],
            })
        selected = {
            identifier: attributes
            for identifier, attributes in (apps or {}).items()
            if identifier.startswith("com.kdt.livecontainer") or identifier.startswith("com.libnoname.noname")
        }
        return {"ok": True, "apps": selected}
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


async def accessibility_captions(rsd: Any) -> list[str]:
    captions: list[str] = []
    async with AccessibilityAudit(rsd) as accessibility:
        async for focused in accessibility.iter_elements():
            item = focused.to_dict()
            caption = str(item.get("caption") or "").strip()
            if caption:
                captions.append(caption)
    return captions


async def swipe_up(rsd: Any) -> None:
    x = 32768
    start_y = 63500
    end_y = 20000
    steps = 40
    async with touch_session(rsd) as service:
        for index in range(steps):
            fraction = index / (steps - 1)
            y = round(start_y + (end_y - start_y) * fraction)
            await service.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y)
            await asyncio.sleep(0.04)
        await service.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x, end_y)


async def wake_and_unlock(udid: str) -> dict[str, Any]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        await press_home(rsd)
        await asyncio.sleep(1.2)
        captions = await accessibility_captions(rsd)
        locked = captions_report_locked(captions)
        # Report the state instead of raising: the caller decides whether to
        # enter the passcode, and a LaunchError here would hide the real state.
        return {"ok": True, "lockedBefore": locked, "unlocked": not locked}
    finally:
        await tunnel.aclose()


async def unlock_with_passcode(udid: str, passcode: str) -> dict[str, Any]:
    # Validates the digits and rejects anything that is not 4-12 numbers.
    passcode_usages(passcode)
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        await press_home(rsd)
        await asyncio.sleep(1.0)

        if not await open_passcode_keypad(rsd):
            # No keypad after several wake attempts: the device is already
            # unlocked, or its lock screen uses a layout we do not recognise.
            # Report success so the caller can retry the app launch; a genuinely
            # locked device will make that launch fail with a clear error.
            print(
                "[unlock] no passcode keypad appeared; treating the iPad as unlocked",
                file=sys.stderr,
                flush=True,
            )
            return {"ok": True, "unlocked": True, "alreadyUnlocked": True}
        # Never print the value itself; the length is enough to diagnose a stale
        # secret (this iPad expects six digits).
        print(f"[unlock] passcode keypad is open; entering {len(passcode)} digits", file=sys.stderr, flush=True)
        await asyncio.sleep(1.5)

        # A hardware keyboard is the most reliable input once the keypad is up:
        # no coordinates, no animation race. If it does not register anything we
        # fall back to tapping the on-screen keys below.
        async with touch_session(rsd) as service:
            keyboard_id = await service.create_keyboard_service()
            for usage in passcode_usages(passcode):
                await service.send_keyboard(keyboard_id, [usage])
                await asyncio.sleep(0.12)
                await service.send_keyboard(keyboard_id, [])
                await asyncio.sleep(0.12)
            await service.send_keyboard(keyboard_id, [0x28])
            await asyncio.sleep(0.05)
            await service.send_keyboard(keyboard_id, [])

        await asyncio.sleep(2.5)
        captions = await accessibility_captions(rsd)
        typed = passcode_digit_count(captions)
        print(
            f"[unlock] after keyboard entry: digits={typed} locked={captions_report_locked(captions)}",
            file=sys.stderr,
            flush=True,
        )
        if not captions_report_locked(captions) and not captions_show_passcode_keypad(captions):
            return {"ok": True, "unlocked": True}
        if typed:
            raise LaunchError(
                "The keyboard entered part of the passcode but the iPad stayed locked; "
                "refusing to mix keyboard and tap input. Verify NONAME_IOS_PASSCODE."
            )

        # Keyboard input did not reach the field: tap the on-screen keys instead.
        async with ScreenCaptureService(rsd) as service:
            response = await service.capture_screenshot()
        if screenshot_is_portrait(response["image"]):
            raise LaunchError(
                "The iPad is in portrait orientation; automatic passcode entry "
                "only supports the landscape keypad layout."
            )

        for index, digit in enumerate(passcode):
            point = PASSCODE_KEY_TAPS.get(digit)
            if point is None:
                raise LaunchError(f"Unsupported passcode digit: {digit!r}")
            await tap(rsd, point)
            await asyncio.sleep(0.7)
            current = await read_passcode_digit_count(rsd)
            print(f"[unlock] tap {index + 1}/{len(passcode)}: digits={current}", file=sys.stderr, flush=True)
            if current is None:
                break

        await asyncio.sleep(2.5)
        captions = await accessibility_captions(rsd)
        print(f"[unlock] lock-screen state after entry: {captions[:3]}", file=sys.stderr, flush=True)
        if captions_report_locked(captions) or captions_show_passcode_keypad(captions):
            raise LaunchError(
                "Passcode entry did not unlock the iPad. Verify NONAME_IOS_PASSCODE; "
                "repeated wrong attempts will lock the device."
            )
        return {"ok": True, "unlocked": True}
    finally:
        await tunnel.aclose()


async def press_button(udid: str, button: str) -> dict[str, Any]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        usage = {
            "home": (0x0C, 0x40, 0.05),
            "lock": (0x0C, 0x30, 0.5),
        }[button]
        usage_page, usage_code, hold = usage
        async with IndigoHIDService(rsd) as service:
            await service.send_button(usage_page, usage_code, HID_BUTTON_STATE_DOWN)
            await asyncio.sleep(hold)
            await service.send_button(usage_page, usage_code, HID_BUTTON_STATE_UP)
        return {"ok": True, "button": button}
    finally:
        await tunnel.aclose()


async def hold_awake(udid: str, timeout: int) -> dict[str, Any]:
    """Hold a PreventUserIdleSystemSleep assertion for the whole ``timeout``.

    The assertion is created before the sleep and released only when the
    surrounding context managers exit, so the device stays awake for the full
    window. This coroutine blocks for ``timeout`` seconds by design: a caller
    that should stay responsive runs it as a separate process, and a caller that
    wants continuous coverage passes a short window and renews it right away.
    """
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    held_seconds = min(MAX_AWAKE_HOLD_SECONDS, max(1, int(timeout)))
    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        async with PowerAssertionService(rsd) as service:
            async with service.create_power_assertion(
                "PreventUserIdleSystemSleep",
                "Noname Deployment",
                held_seconds,
            ):
                # Sleep inside the assertion context so the assertion stays
                # active; returning earlier releases it immediately and the
                # device idles back to sleep.
                await asyncio.sleep(held_seconds)
        return {
            "ok": True,
            "timeout": held_seconds,
            "assertions": ["PreventUserIdleSystemSleep"],
        }
    finally:
        await tunnel.aclose()


async def container_file_action(
    udid: str,
    bundle_id: str,
    *,
    list_path: str | None,
    read_path: str | None,
) -> dict[str, Any]:
    userspace_tunnel._create_no_root_tunnel_provider = create_mobdev2_tunnel_provider

    tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=udid, autopair=False)
    try:
        rsd = await tunnel.aopen()
        async with AppServiceService(rsd) as app_service:
            resolved_bundle_id = resolve_bundle_id(bundle_id, await installed_bundle_ids(app_service))
        async with FileServiceService(
            rsd,
            Domain.APP_DATA_CONTAINER,
            identifier=resolved_bundle_id,
        ) as service:
            result: dict[str, Any] = {"ok": True, "bundleID": resolved_bundle_id}
            if list_path is not None:
                result["entries"] = await service.retrieve_directory_list(list_path)
            if read_path is not None:
                data = await service.retrieve_file(read_path)
                try:
                    result["text"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    result["base64"] = base64.b64encode(data).decode()
            return result
    finally:
        await tunnel.aclose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True)
    parser.add_argument("--bundle-id")
    parser.add_argument("--token-env")
    parser.add_argument("--list-apps", action="store_true")
    parser.add_argument("--app-info", action="store_true")
    parser.add_argument("--container-id")
    parser.add_argument("--container-list")
    parser.add_argument("--container-read")
    parser.add_argument("--open-url")
    parser.add_argument("--configure-token-env")
    parser.add_argument("--skip-install-confirmation", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--tap", nargs=2, type=int, metavar=("X", "Y"))
    parser.add_argument("--accessibility-list", action="store_true")
    parser.add_argument("--accessibility-press")
    parser.add_argument("--wake", action="store_true")
    parser.add_argument("--unlock-passcode-env")
    parser.add_argument("--button", choices=("home", "lock"))
    parser.add_argument(
        "--hold-awake",
        type=int,
        metavar="SECONDS",
        help="Hold a PreventUserIdleSystemSleep assertion and block for SECONDS (max 300 is recommended)",
    )
    parser.add_argument("argument", nargs="*")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.environ.get("NONAME_DEPLOY_PRODUCTION") == "1":
        # --app-info stays available in production: the deployment recovery path
        # needs the installed CFBundleVersion to decide whether an interrupted
        # job already installed the expected build. It only reads metadata.
        debug_only = any(
            (
                args.list_apps,
                args.container_id is not None,
                args.container_list is not None,
                args.container_read is not None,
                args.screenshot is not None,
                args.tap is not None,
                args.accessibility_list,
                args.accessibility_press is not None,
                args.skip_install_confirmation,
            )
        )
        if debug_only:
            print(
                "error: developer inspection options are disabled in the production deployment action",
                file=sys.stderr,
            )
            return 2
    if args.configure_token_env:
        token = os.environ.get(args.configure_token_env, "")
        if len(token) < 32:
            print(f"error: {args.configure_token_env} must contain a deployment token", file=sys.stderr)
            return 1
        query = urllib.parse.urlencode({"token": token})
        args.open_url = f"sidestore://noname-deploy/configure?{query}"
    if (
        not args.list_apps
        and not args.app_info
        and args.container_list is None
        and args.container_read is None
        and args.screenshot is None
        and args.tap is None
        and not args.accessibility_list
        and args.accessibility_press is None
        and not args.wake
        and args.unlock_passcode_env is None
        and args.button is None
        and args.hold_awake is None
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
        elif args.app_info:
            result = asyncio.run(app_info(args.udid))
        elif args.container_list is not None or args.container_read is not None:
            if not args.container_id:
                print("error: --container-id is required for container access", file=sys.stderr)
                return 1
            result = asyncio.run(
                container_file_action(
                    args.udid,
                    args.container_id,
                    list_path=args.container_list,
                    read_path=args.container_read,
                )
            )
        elif args.open_url:
            assert args.bundle_id is not None
            arguments = ["-isInstallConfirmationEnabled", "false"] if args.skip_install_confirmation else []
            result = asyncio.run(open_url(args.udid, args.bundle_id, args.open_url, arguments))
            if args.configure_token_env:
                result["payloadURL"] = "sidestore://noname-deploy/configure?token=<redacted>"
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
        elif args.wake:
            result = asyncio.run(wake_and_unlock(args.udid))
        elif args.unlock_passcode_env:
            passcode = os.environ.get(args.unlock_passcode_env, "")
            result = asyncio.run(unlock_with_passcode(args.udid, passcode))
        elif args.button:
            result = asyncio.run(press_button(args.udid, args.button))
        elif args.hold_awake:
            result = asyncio.run(hold_awake(args.udid, args.hold_awake))
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
