#!/usr/bin/env python3
"""Deploy an IPA through the authenticated Noname endpoint in built-in SideStore.

The deployment survives the iPad turning its screen off:

1. ``keep_device_awake.py`` runs in the background for the whole deployment and
   holds a ``PreventUserIdleSystemSleep`` assertion, so the device does not idle
   into sleep while SideStore downloads and verifies the IPA.
2. The display is locked only once the job reaches the install stage, where
   SideStore keeps itself alive with its silent-audio background task.
3. ``GET /v1/hello`` is polled as a liveness probe. When the endpoint stops
   answering because iOS suspended the host, the runner wakes the iPad,
   unlocks it, restarts LiveContainer+SideStore with a fresh token and resumes
   the deployment with a new job.

Environment variables
---------------------
``NONAME_LOCK_DURING_DEPLOY``
    ``1`` (default) presses the device lock button once the deployment job
    reaches the install stage so the screen is off while the installation
    finishes. ``0`` keeps the display on.
``NONAME_MAX_RECOVERIES``
    Maximum number of automatic wake/unlock/restart cycles per deployment.
    Defaults to ``2``.
``NONAME_KEEP_AWAKE``
    ``1`` (default) runs the assertion renewer in the background for the whole
    deployment. ``0`` disables it.
``NONAME_IOS_PASSCODE``
    Optional lock-screen passcode. It is read from the environment only and is
    never printed: command output and error messages redact it.
``NONAME_ALIVE_TIMEOUT``
    Per-request timeout in seconds for the ``/v1/hello`` liveness probe.
    Defaults to ``2.0``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import plistlib
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEPLOY_PORT = 49321
SERVICE_TYPE = "_noname-deploy._tcp"
SERVICE_DNS_TYPE = f"{SERVICE_TYPE}.local."
DEFAULT_BUNDLE_ID = "com.libnoname.noname"
HOST_BUNDLE_ID = "com.kdt.livecontainer"
LOCAL_DEV_VPN_BUNDLE_ID = "com.jkcoxson.LocalDevVPN"
LOCAL_DEV_VPN_ENABLE_URL = "localdevvpn://enable"
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Deployment state machine tuning.
POLL_INTERVAL = 2.0
RECOVERY_FAILURE_THRESHOLD = 3
DEFAULT_MAX_RECOVERIES = 2
DEFAULT_ALIVE_TIMEOUT = 2.0
RECOVERY_SERVER_TIMEOUT = 180.0
KEEP_AWAKE_INTERVAL = 30
KEEP_AWAKE_ASSERTION_TIMEOUT = 300
INSTALL_STAGE_MARKERS = ("installing ipa", "succeeded")
# A CFBundleVersion is only trusted as a per-build identifier when it is long
# enough to be unique. The iOS project still hard-codes
# CURRENT_PROJECT_VERSION = 1, so "1"/"1.0" must never satisfy a version match.
MIN_UNIQUE_VERSION_LENGTH = 8
NON_UNIQUE_VERSIONS = frozenset({"1", "1.0"})


class DeploymentError(RuntimeError):
    pass


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment flag, accepting 0/1 and true/false spellings."""
    raw = os.environ.get(name, "").strip().casefold()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def env_int(name: str, default: int) -> int:
    """Read an integer environment value, falling back to the default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"warning: {name} is not an integer ({raw!r}); using {default}", flush=True)
        return default


def env_float(name: str, default: float) -> float:
    """Read a float environment value, falling back to the default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"warning: {name} is not a number ({raw!r}); using {default}", flush=True)
        return default


def redact_text(text: str, values: tuple[str, ...]) -> str:
    """Replace every non-empty secret in ``values`` with a placeholder."""
    rendered = text
    for value in values:
        if value:
            rendered = rendered.replace(value, "<redacted>")
    return rendered


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_request(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    body_digest = hashlib.sha256(body).hexdigest()
    return "\n".join((method, path, timestamp, nonce, body_digest)).encode()


def find_dicts(value: Any, predicate):
    if isinstance(value, dict):
        if predicate(value):
            yield value
        for child in value.values():
            yield from find_dicts(child, predicate)
    elif isinstance(value, list):
        for child in value:
            yield from find_dicts(child, predicate)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Return the last JSON object printed on ``text``, ignoring other lines."""
    for line in reversed((text or "").splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def run(
    command: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
    env: dict[str, str] | None = None,
    redact: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, env=env)
    if check and result.returncode != 0:
        display_command = redact_text(" ".join(command), redact)
        stdout = redact_text(result.stdout, redact)
        stderr = redact_text(result.stderr, redact)
        raise DeploymentError(
            f"Command failed ({result.returncode}): {display_command}\n"
            f"{stdout}\n{stderr}"
        )
    return result


def run_with_retries(
    command: list[str],
    *,
    attempts: int = 4,
    delay: float = 2,
    timeout: int = 60,
    env: dict[str, str] | None = None,
    redact: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return run(
                command,
                timeout=timeout,
                env=env,
                redact=redact,
            )
        except Exception as error:
            last_error = error
            if "remains locked" in str(error):
                raise
            if attempt + 1 >= attempts:
                break
            time.sleep(min(delay * (attempt + 1), 10))
    assert last_error is not None
    raise last_error


def parse_discovered_devices(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        raise DeploymentError("pymobiledevice3 discovery returned an unexpected payload")
    candidates = []
    for device in payload["devices"]:
        if not isinstance(device, dict):
            continue
        product_type = str(device.get("productType") or "")
        device_class = str(device.get("deviceClass") or "")
        identifier = str(device.get("identifier") or "")
        if not identifier:
            continue
        if device_class != "iPad" and not product_type.startswith("iPad"):
            continue
        candidates.append(
            {
                "identifier": identifier,
                "name": str(device.get("name") or product_type or identifier),
                "host": device.get("host"),
            }
        )
    return candidates


def select_pymobiledevice3_device() -> dict[str, Any]:
    helper = Path(__file__).with_name("discover_ios_devices.py")
    result = run(
        [sys.executable, str(helper), "--timeout", "5"],
        timeout=30,
        check=False,
    )
    try:
        candidates = parse_discovered_devices(json.loads(result.stdout))
    except json.JSONDecodeError as error:
        raise DeploymentError(f"Could not parse pymobiledevice3 discovery output: {result.stdout}") from error

    if len(candidates) != 1:
        names = ", ".join(item["name"] or item["identifier"] for item in candidates) or "none"
        raise DeploymentError(
            f"Expected exactly one paired iPad from pymobiledevice3 discovery, found {len(candidates)}: {names}. "
            "Configure ipad_device_id with the target UDID."
        )
    return candidates[0]


def select_device(explicit_device: str | None) -> dict[str, Any]:
    if explicit_device:
        return {"identifier": explicit_device, "name": "configured device"}

    if sys.platform == "darwin":
        with tempfile.TemporaryDirectory(prefix="noname-devicectl-") as directory:
            output = Path(directory) / "devices.json"
            run(["xcrun", "devicectl", "list", "devices", "--json-output", str(output), "--quiet"], timeout=30)
            payload = json.loads(output.read_text(encoding="utf-8"))

        candidates = []
        for device in find_dicts(payload, lambda item: "identifier" in item and "deviceProperties" in item):
            properties = device.get("deviceProperties") or {}
            connection = device.get("connectionProperties") or {}
            name = str(properties.get("name") or "")
            if connection.get("pairingState") not in (None, "paired"):
                continue
            if connection.get("tunnelState") not in (None, "connected"):
                continue
            if "ipad" not in name.lower():
                continue
            candidates.append(
                {
                    "identifier": str(device["identifier"]),
                    "name": name,
                    "host": connection.get("dnsName"),
                }
            )

        if len(candidates) != 1:
            names = ", ".join(item["name"] or item["identifier"] for item in candidates) or "none"
            raise DeploymentError(
                f"Expected exactly one paired iPad, found {len(candidates)}: {names}. "
                "Use --device-id to select one explicitly."
            )
        return candidates[0]

    return select_pymobiledevice3_device()


def launch_local_dev_vpn(device_id: str) -> None:
    if sys.platform == "darwin":
        run(
            [
                "xcrun",
                "devicectl",
                "device",
                "process",
                "launch",
                "--device",
                device_id,
                "--terminate-existing",
                LOCAL_DEV_VPN_BUNDLE_ID,
            ],
            timeout=60,
        )
        return

    environment = os.environ.copy()
    helper = Path(__file__).with_name("launch_ios_app.py")
    run_with_retries(
        [
            sys.executable,
            str(helper),
            "--udid",
            device_id,
            "--button",
            "home",
        ],
        attempts=4,
        timeout=120,
        env=environment,
        redact=(device_id,),
    )
    time.sleep(1)
    print("Launching LocalDevVPN and waiting for its Auto-connect setting", flush=True)
    run_with_retries(
        [
            sys.executable,
            str(helper),
            "--udid",
            device_id,
            "--bundle-id",
            LOCAL_DEV_VPN_BUNDLE_ID,
        ],
        attempts=3,
        timeout=120,
        env=environment,
        redact=(device_id,),
    )


def enable_local_dev_vpn(device_id: str) -> None:
    print("No utun detected; enabling LocalDevVPN through localdevvpn://enable", flush=True)
    if sys.platform == "darwin":
        run(
            [
                "xcrun",
                "devicectl",
                "device",
                "process",
                "launch",
                "--device",
                device_id,
                "--terminate-existing",
                "--payload-url",
                LOCAL_DEV_VPN_ENABLE_URL,
                LOCAL_DEV_VPN_BUNDLE_ID,
            ],
            timeout=60,
        )
        return

    helper = Path(__file__).with_name("launch_ios_app.py")
    environment = os.environ.copy()
    run_with_retries(
        [
            sys.executable,
            str(helper),
            "--udid",
            device_id,
            "--bundle-id",
            LOCAL_DEV_VPN_BUNDLE_ID,
            "--open-url",
            LOCAL_DEV_VPN_ENABLE_URL,
        ],
        attempts=4,
        timeout=120,
        env=environment,
        redact=(device_id,),
    )


def launch_configured_side_store(device_id: str, token: str) -> None:
    if sys.platform == "darwin":
        launch_local_dev_vpn(device_id)
        time.sleep(10)
        query = urllib.parse.urlencode({"token": token})
        payload_url = f"sidestore://noname-deploy/configure?{query}"
        run(
            [
                "xcrun",
                "devicectl",
                "device",
                "process",
                "launch",
                "--device",
                device_id,
                "--terminate-existing",
                "--payload-url",
                payload_url,
                HOST_BUNDLE_ID,
            ],
            timeout=60,
            redact=(token,),
        )
        return

    environment = os.environ.copy()
    helper = Path(__file__).with_name("launch_ios_app.py")
    discovery_helper = Path(__file__).with_name("discover_ios_devices.py")
    online_timeout = float(os.environ.get("NONAME_DEVICE_ONLINE_TIMEOUT", "300"))
    deadline = time.monotonic() + online_timeout
    while True:
        discovery = run(
            [
                sys.executable,
                str(discovery_helper),
                "--udid",
                device_id,
                "--timeout",
                "3",
            ],
            timeout=30,
            env=environment,
            check=False,
        )
        try:
            devices = json.loads(discovery.stdout).get("devices", [])
        except (json.JSONDecodeError, AttributeError):
            devices = []
        if any(device.get("identifier") == device_id for device in devices if isinstance(device, dict)):
            break
        if time.monotonic() >= deadline:
            raise DeploymentError(f"Paired iPad {device_id} did not become reachable within {online_timeout:g}s")
        time.sleep(2)

    # The deployment itself keeps the device awake through keep_awake(), which
    # holds its assertion for a bounded window and renews it. An inline
    # --hold-awake call here would block this process for the whole window.
    try:
        launch_local_dev_vpn(device_id)
    except DeploymentError:
        passcode = environment.get("NONAME_IOS_PASSCODE", "").strip()
        if not passcode:
            raise
        print("iPad is locked; unlocking with the configured passcode", flush=True)
        run_with_retries(
            [
                sys.executable,
                str(helper),
                "--udid",
                device_id,
                "--unlock-passcode-env",
                "NONAME_IOS_PASSCODE",
            ],
            attempts=3,
            timeout=120,
            env=environment,
            redact=(device_id, passcode),
        )
        launch_local_dev_vpn(device_id)
    time.sleep(10)

    environment["NONAME_DEPLOY_TOKEN"] = token
    run_with_retries(
        [
            sys.executable,
            str(helper),
            "--udid",
            device_id,
            "--bundle-id",
            HOST_BUNDLE_ID,
            "--configure-token-env",
            "NONAME_DEPLOY_TOKEN",
        ],
        attempts=3,
        timeout=120,
        env=environment,
        redact=(device_id, token),
    )


def terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                return


def read_process_lines_until(process: subprocess.Popen[str], deadline: float):
    if process.stdout is None:
        return
    while time.monotonic() < deadline:
        readable, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
        if not readable:
            return
        line = process.stdout.readline()
        if not line:
            return
        yield line


def discover_bonjour_with_dns_sd(timeout: float) -> tuple[str, int]:
    browse = subprocess.Popen(
        ["dns-sd", "-B", SERVICE_TYPE, "local."],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    service_name: str | None = None
    deadline = time.monotonic() + timeout
    try:
        for line in read_process_lines_until(browse, deadline):
            if " Add " in line:
                parts = line.strip().split()
                if len(parts) >= 7:
                    service_name = parts[-1]
                    break
    finally:
        terminate_process(browse)

    if not service_name:
        raise DeploymentError("SideStore deployment service was not found over Bonjour")

    resolve = subprocess.Popen(
        ["dns-sd", "-L", service_name, SERVICE_TYPE, "local."],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    deadline = time.monotonic() + timeout
    try:
        for line in read_process_lines_until(resolve, deadline):
            match = re.search(r"can be reached at ([^:\s]+):(\d+)", line)
            if not match:
                continue
            hostname = match.group(1).rstrip(".")
            port = int(match.group(2))
            try:
                infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            except OSError:
                continue
            for info in infos:
                address = info[4][0]
                if ":" not in address:
                    return address, port
    finally:
        terminate_process(resolve)

    raise DeploymentError(f"Could not resolve SideStore deployment host '{service_name}'")


def discover_bonjour_with_zeroconf(timeout: float) -> tuple[str, int]:
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as error:
        raise DeploymentError(
            "DNS-SD discovery requires dns-sd on macOS or the zeroconf Python package on Linux"
        ) from error

    names: set[str] = set()

    class Listener(ServiceListener):
        def add_service(self, zeroconf: Any, service_type: str, name: str) -> None:
            names.add(name)

        def update_service(self, zeroconf: Any, service_type: str, name: str) -> None:
            names.add(name)

        def remove_service(self, zeroconf: Any, service_type: str, name: str) -> None:
            names.discard(name)

    zeroconf = Zeroconf()
    browser = ServiceBrowser(zeroconf, SERVICE_DNS_TYPE, Listener())
    deadline = time.monotonic() + timeout
    endpoints: set[tuple[str, int]] = set()
    try:
        while time.monotonic() < deadline:
            for name in tuple(names):
                info = zeroconf.get_service_info(SERVICE_DNS_TYPE, name, timeout=500)
                if not info:
                    continue
                for address in info.parsed_addresses():
                    if ":" in address:
                        continue
                    endpoints.add((address, int(info.port)))
            if endpoints:
                break
            time.sleep(0.25)
    finally:
        browser.cancel()
        zeroconf.close()

    if len(endpoints) != 1:
        rendered = ", ".join(f"{host}:{port}" for host, port in sorted(endpoints)) or "none"
        raise DeploymentError(
            f"Expected exactly one SideStore deployment endpoint over Bonjour, found {len(endpoints)}: {rendered}"
        )
    return endpoints.pop()


def discover_bonjour_service(timeout: float) -> tuple[str, int]:
    if sys.platform == "darwin" and shutil.which("dns-sd"):
        return discover_bonjour_with_dns_sd(timeout)
    return discover_bonjour_with_zeroconf(timeout)


def local_address_for(remote_host: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((remote_host, 9))
        address = probe.getsockname()[0]
    finally:
        probe.close()
    if not address or address.startswith("127."):
        raise DeploymentError("Could not determine the LAN address of the deployment runner")
    return address


class IPARequestHandler(BaseHTTPRequestHandler):
    ipa_path: Path
    expected_path: str

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def _serve(self, *, send_body: bool) -> None:
        if urllib.parse.urlsplit(self.path).path != self.expected_path:
            self.send_error(404)
            return
        stat = self.ipa_path.stat()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(stat.st_size))
        self.end_headers()
        if send_body:
            with self.ipa_path.open("rb") as file:
                shutil.copyfileobj(file, self.wfile)

    def log_message(self, format: str, *args: Any) -> None:
        return


def wait_for_server(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with LOCAL_OPENER.open(f"http://{host}:{port}/v1/hello", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as error:
            last_error = error
        time.sleep(0.5)
    raise DeploymentError(f"SideStore deployment endpoint did not become ready: {last_error}")


def signed_request(
    host: str,
    port: int,
    secret: str,
    payload: dict[str, Any],
    *,
    method: str,
    path: str,
    body: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    body = body if body is not None else json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signature = hmac.new(
        secret.encode(),
        canonical_request(method, path, timestamp, nonce, body),
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Noname-Timestamp": timestamp,
            "X-Noname-Nonce": nonce,
            "X-Noname-Signature": signature,
        },
    )
    try:
        with LOCAL_OPENER.open(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read())
        except Exception:
            detail = {"message": error.reason}
        return error.code, detail


def is_vpn_connection_error(message: str) -> bool:
    normalized = message.casefold()
    return (
        "no utun interface" in normalized
        or "localdevvpn is not connected" in normalized
        or "vpn connection error" in normalized
    )


def format_flag(value: Any) -> str:
    """Render an optional boolean telemetry field for the log."""
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def describe_power_state(hello: dict[str, Any]) -> str:
    """Render the optional background/audio flags of a ``/v1/hello`` reply.

    Older SideStore builds do not report these fields; they are logged as
    ``unknown`` instead of failing the deployment.
    """
    return (
        f"backgroundActive={format_flag(hello.get('backgroundActive'))} "
        f"audioActive={format_flag(hello.get('audioActive'))}"
    )


def is_install_stage(message: str) -> bool:
    """Return True once the job message reached the install or success stage."""
    normalized = (message or "").casefold()
    return any(marker in normalized for marker in INSTALL_STAGE_MARKERS)


def is_unique_build_version(version: str | None) -> bool:
    """Return True when ``version`` can identify one specific build.

    The iOS project currently pins ``CURRENT_PROJECT_VERSION = 1``, so the same
    value is shared by every local build. Only long version strings are trusted
    for the "this build is already installed" shortcut; the nightly workflow
    injects such a per-build value. Anything else falls back to a real reinstall,
    which is slow but always correct.
    """
    if not version:
        return False
    candidate = version.strip()
    if candidate in NON_UNIQUE_VERSIONS:
        return False
    return len(candidate) >= MIN_UNIQUE_VERSION_LENGTH


def should_recover(
    *,
    alive: bool,
    failures: int,
    locked: bool,
    recoveries: int,
    max_recoveries: int,
) -> bool:
    """Decide whether a suspended SideStore host should be recovered.

    Recovery is only attempted after the display was locked (that is the point
    where iOS may suspend the app), after ``RECOVERY_FAILURE_THRESHOLD``
    consecutive liveness failures, and while the recovery budget lasts.
    """
    if alive:
        return False
    if not locked:
        return False
    if failures < RECOVERY_FAILURE_THRESHOLD:
        return False
    return recoveries < max_recoveries


def hello_state(
    host: str,
    port: int,
    timeout: float = DEFAULT_ALIVE_TIMEOUT,
) -> dict[str, Any] | None:
    """Return the parsed ``/v1/hello`` payload, or None when it is unreachable."""
    request = urllib.request.Request(f"http://{host}:{port}/v1/hello", method="GET")
    try:
        with LOCAL_OPENER.open(request, timeout=timeout) as response:
            if not 200 <= int(response.status) < 300:
                return None
            return parse_json_object(response.read().decode("utf-8", "replace"))
    except Exception:
        return None


def probe_alive(host: str, port: int, timeout: float = DEFAULT_ALIVE_TIMEOUT) -> bool:
    """Report whether SideStore answers ``/v1/hello`` with ``ok``."""
    state = hello_state(host, port, timeout)
    return bool(state and state.get("ok"))


def ipa_build_version(ipa_path: Path) -> str | None:
    """Read ``CFBundleVersion`` from ``Payload/*.app/Info.plist`` inside an IPA."""
    try:
        with zipfile.ZipFile(ipa_path) as archive:
            for name in archive.namelist():
                parts = name.split("/")
                if len(parts) != 3:
                    continue
                if parts[0] != "Payload" or parts[2] != "Info.plist":
                    continue
                if not parts[1].endswith(".app"):
                    continue
                with archive.open(name) as handle:
                    info = plistlib.load(handle)
                if not isinstance(info, dict):
                    continue
                version = info.get("CFBundleVersion")
                if isinstance(version, str) and version.strip():
                    return version.strip()
                return None
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, plistlib.InvalidFileException):
        return None
    return None


def installed_app_version(device_id: str, bundle_id: str) -> str | None:
    """Read the installed ``CFBundleVersion`` for ``bundle_id`` through the helper."""
    helper = Path(__file__).with_name("launch_ios_app.py")
    environment = os.environ.copy()
    passcode = environment.get("NONAME_IOS_PASSCODE", "").strip()
    result = run(
        [sys.executable, str(helper), "--udid", device_id, "--app-info"],
        timeout=120,
        check=False,
        env=environment,
        redact=(device_id, passcode),
    )
    if result.returncode != 0:
        detail = redact_text((result.stderr or "").strip(), (device_id, passcode))
        print(f"[recover] could not read installed app info: {detail or result.returncode}", flush=True)
        return None
    payload = parse_json_object(result.stdout)
    if payload is None:
        return None
    apps = payload.get("apps")
    if not isinstance(apps, dict):
        return None
    candidates = [bundle_id]
    candidates.extend(key for key in apps if str(key).startswith(f"{bundle_id}."))
    for key in candidates:
        attributes = apps.get(key)
        if not isinstance(attributes, dict):
            continue
        version = attributes.get("CFBundleVersion")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return None


def ensure_device_unlocked(device_id: str) -> None:
    """Wake the iPad and unlock it, using ``NONAME_IOS_PASSCODE`` when needed."""
    helper = Path(__file__).with_name("launch_ios_app.py")
    environment = os.environ.copy()
    passcode = environment.get("NONAME_IOS_PASSCODE", "").strip()
    redactions = (device_id, passcode)

    print("[recover] waking the iPad", flush=True)
    wake = run(
        [sys.executable, str(helper), "--udid", device_id, "--wake"],
        timeout=180,
        check=False,
        env=environment,
        redact=redactions,
    )
    payload = parse_json_object(wake.stdout) or {}
    unlocked = payload.get("unlocked") is True
    already_awake = payload.get("lockedBefore") is False
    if wake.returncode == 0 and (unlocked or already_awake):
        print(f"[recover] the iPad is awake (unlocked={format_flag(unlocked)})", flush=True)
        return

    if not passcode:
        raise DeploymentError(
            "The iPad is locked and NONAME_IOS_PASSCODE is not configured. "
            "Enter the passcode on the device or disable idle auto-lock."
        )

    print("[recover] the iPad is locked; unlocking with the configured passcode", flush=True)
    unlock = run(
        [
            sys.executable,
            str(helper),
            "--udid",
            device_id,
            "--unlock-passcode-env",
            "NONAME_IOS_PASSCODE",
        ],
        timeout=180,
        check=False,
        env=environment,
        redact=redactions,
    )
    if unlock.returncode != 0:
        raise DeploymentError(
            "Automatic passcode unlock failed; verify NONAME_IOS_PASSCODE and the iPad lock state"
        )
    print("[recover] passcode unlock finished", flush=True)


@contextlib.contextmanager
def keep_awake(device_id: str):
    """Run the assertion renewer in the background for the whole deployment.

    Yields the ``Popen`` handle (or None when the helper is disabled) so the
    caller can notice an early exit. The helper holds a
    ``PreventUserIdleSystemSleep`` assertion for a bounded window and renews it,
    which keeps the runner responsive instead of blocking on one long hold.
    """
    if not env_flag("NONAME_KEEP_AWAKE", True):
        print("[awake] keep-awake helper disabled by NONAME_KEEP_AWAKE", flush=True)
        yield None
        return
    if sys.platform == "darwin":
        print("[awake] macOS runners keep the device awake through devicectl", flush=True)
        yield None
        return

    helper = Path(__file__).with_name("keep_device_awake.py")
    command = [
        sys.executable,
        str(helper),
        "--interval",
        str(KEEP_AWAKE_INTERVAL),
        "--assertion-timeout",
        str(KEEP_AWAKE_ASSERTION_TIMEOUT),
    ]
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        print(
            f"[awake] helper started (pid {process.pid}) holding "
            f"{KEEP_AWAKE_ASSERTION_TIMEOUT}s assertions every {KEEP_AWAKE_INTERVAL}s for {device_id}",
            flush=True,
        )
        yield process
    finally:
        if process is not None:
            terminate_process(process)
            print("[awake] helper stopped", flush=True)


def lock_device_display(device_id: str) -> None:
    if not env_flag("NONAME_LOCK_DURING_DEPLOY", True):
        return
    if sys.platform == "darwin":
        print("[phase] iOS background deployment is enabled; use the device power button to lock it", flush=True)
        return

    helper = Path(__file__).with_name("launch_ios_app.py")
    run_with_retries(
        [
            sys.executable,
            str(helper),
            "--udid",
            device_id,
            "--button",
            "lock",
        ],
        attempts=3,
        timeout=120,
        env=os.environ.copy(),
        redact=(device_id, os.environ.get("NONAME_IOS_PASSCODE", "")),
    )
    print("[phase] locked the iPad display; SideStore continues with its silent-audio background task", flush=True)


def restore_display_off(device_id: str) -> None:
    """Press the lock button once so the device returns to its screen-off state."""
    if not env_flag("NONAME_LOCK_DURING_DEPLOY", True):
        return
    if sys.platform == "darwin":
        print("[phase] press the device power button to lock it again", flush=True)
        return

    helper = Path(__file__).with_name("launch_ios_app.py")
    passcode = os.environ.get("NONAME_IOS_PASSCODE", "").strip()
    result = run(
        [
            sys.executable,
            str(helper),
            "--udid",
            device_id,
            "--button",
            "lock",
        ],
        timeout=120,
        check=False,
        env=os.environ.copy(),
        redact=(device_id, passcode),
    )
    if result.returncode == 0:
        print("[phase] restored the screen-off state with one lock-button press", flush=True)
    else:
        detail = redact_text((result.stderr or "").strip(), (device_id, passcode))
        print(f"[phase] could not restore the screen-off state: {detail or result.returncode}", flush=True)


def submit_job(
    host: str,
    port: int,
    token: str,
    payload: dict[str, Any],
    *,
    label: str,
) -> str:
    """POST ``/v1/deploy`` and return the accepted job ID."""
    status, response = signed_request(
        host,
        port,
        token,
        payload,
        method="POST",
        path="/v1/deploy",
    )
    if status != 202 or not response.get("ok"):
        raise DeploymentError(f"Deployment request was rejected: {response}")
    job_id = response.get("jobID")
    if not job_id:
        raise DeploymentError("SideStore did not return a deployment job ID")
    print(f"[phase] {label} job accepted: {job_id}", flush=True)
    return str(job_id)


def recover(
    *,
    host: str,
    port: int,
    token: str,
    device_id: str,
    payload: dict[str, Any],
    ipa_path: Path,
    message: str,
    recoveries: int,
    max_recoveries: int,
) -> str | None:
    """Wake, unlock and restart the SideStore host, then resume the deployment.

    Returns the new job ID, or None when the interrupted job had already
    installed the expected build and no reinstall is necessary.
    """
    if recoveries >= max_recoveries:
        raise DeploymentError(
            f"SideStore stopped answering and the recovery budget of {max_recoveries} is exhausted"
        )
    print(
        f"[recover] SideStore stopped answering after {RECOVERY_FAILURE_THRESHOLD} liveness failures; "
        f"starting recovery {recoveries + 1}/{max_recoveries}",
        flush=True,
    )

    if is_install_stage(message):
        expected = ipa_build_version(ipa_path)
        if not expected:
            print("[recover] could not read CFBundleVersion from the IPA; skipping the install-stage shortcut", flush=True)
        elif not is_unique_build_version(expected):
            # Guard against a pinned CURRENT_PROJECT_VERSION: a fixed value such
            # as "1" matches any older install, so comparing it would wrongly
            # report success. Reinstall instead once the workflow injects a
            # unique per-build version.
            print(
                f"[recover] IPA CFBundleVersion {expected!r} is not a unique build number; "
                "skipping the install-stage shortcut and reinstalling",
                flush=True,
            )
        else:
            bundle_id = str(payload.get("bundleID") or DEFAULT_BUNDLE_ID)
            installed = installed_app_version(device_id, bundle_id)
            print(
                f"[recover] install-stage version check: installed={installed or 'unknown'} expected={expected}",
                flush=True,
            )
            if installed == expected:
                print("[recover] the expected build is already installed; skipping the reinstall", flush=True)
                return None

    ensure_device_unlocked(device_id)
    print("[recover] restarting LiveContainer+SideStore with a fresh deployment token", flush=True)
    launch_configured_side_store(device_id, token)
    wait_for_server(host, port, RECOVERY_SERVER_TIMEOUT)
    job_id = submit_job(host, port, token, payload, label="recovery")
    return job_id


def deploy(
    *,
    ipa_path: Path,
    device_id: str | None,
    bundle_id: str,
    host_override: str | None,
    timeout: float,
) -> None:
    if not ipa_path.is_file():
        raise DeploymentError(f"IPA does not exist: {ipa_path}")
    if bundle_id != DEFAULT_BUNDLE_ID:
        raise DeploymentError(f"Unsupported bundle ID: {bundle_id}")

    max_recoveries = max(0, env_int("NONAME_MAX_RECOVERIES", DEFAULT_MAX_RECOVERIES))
    alive_timeout = env_float("NONAME_ALIVE_TIMEOUT", DEFAULT_ALIVE_TIMEOUT)
    lock_during_deploy = env_flag("NONAME_LOCK_DURING_DEPLOY", True)
    print(
        f"[phase] deploy start: ipa={ipa_path.name} timeout={timeout:g}s "
        f"maxRecoveries={max_recoveries} lockDuringDeploy={format_flag(lock_during_deploy)}",
        flush=True,
    )

    device = select_device(device_id)
    token = secrets.token_urlsafe(32)
    session: dict[str, bool] = {"woke": False}

    with keep_awake(device["identifier"]) as awake_process:
        launch_configured_side_store(device["identifier"], token)

        host, port = (
            (host_override, DEPLOY_PORT)
            if host_override
            else discover_bonjour_service(timeout=min(timeout, 30))
        )
        wait_for_server(host, port, timeout=min(timeout, 30))

        digest = sha256_file(ipa_path)
        served_path = f"/ipa/{secrets.token_urlsafe(24)}.ipa"
        runner_ip = local_address_for(host)
        IPARequestHandler.ipa_path = ipa_path
        IPARequestHandler.expected_path = served_path
        server = ThreadingHTTPServer((runner_ip, 0), IPARequestHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        deadline = time.monotonic() + timeout

        try:
            ipa_url = f"http://{runner_ip}:{server.server_address[1]}{served_path}"
            payload: dict[str, Any] = {
                "bundleID": bundle_id,
                "ipaURL": ipa_url,
                "sha256": digest,
            }

            def run_job() -> None:
                job_id = submit_job(host, port, token, payload, label="deployment")
                locked = False
                failures = 0
                recoveries = 0
                last_phase: tuple[str, str] | None = None
                last_alive: bool | None = None
                helper_exit_reported = False
                message = ""

                while time.monotonic() < deadline:
                    if (
                        not helper_exit_reported
                        and awake_process is not None
                        and awake_process.poll() is not None
                    ):
                        print(
                            f"[awake] helper exited with code {awake_process.returncode}; "
                            "continuing without the awake assertion",
                            flush=True,
                        )
                        helper_exit_reported = True

                    hello = hello_state(host, port, alive_timeout)
                    alive = bool(hello and hello.get("ok"))
                    if alive:
                        failures = 0
                        if last_alive is not True:
                            print(
                                f"[alive] SideStore answered /v1/hello ({describe_power_state(hello or {})})",
                                flush=True,
                            )
                    else:
                        failures += 1
                        if failures <= RECOVERY_FAILURE_THRESHOLD or failures % 15 == 0:
                            print(
                                f"[alive] no /v1/hello response ({failures} consecutive failures)",
                                flush=True,
                            )
                    last_alive = alive

                    if should_recover(
                        alive=alive,
                        failures=failures,
                        locked=locked,
                        recoveries=recoveries,
                        max_recoveries=max_recoveries,
                    ):
                        # Mark the device as woken before attempting recovery:
                        # recovery may raise after it already pressed the wake
                        # button, and the finally block must still try to put
                        # the display back to sleep.
                        session["woke"] = True
                        new_job = recover(
                            host=host,
                            port=port,
                            token=token,
                            device_id=device["identifier"],
                            payload=payload,
                            ipa_path=ipa_path,
                            message=message,
                            recoveries=recoveries,
                            max_recoveries=max_recoveries,
                        )
                        recoveries += 1
                        locked = False
                        failures = 0
                        last_alive = None
                        last_phase = None
                        if new_job is None:
                            print(
                                f"[phase] deployment already completed before the suspension "
                                f"({recoveries} recovery attempt(s))",
                                flush=True,
                            )
                            return
                        job_id = new_job
                        continue

                    if not alive:
                        time.sleep(POLL_INTERVAL)
                        continue

                    job_path = f"/v1/jobs/{job_id}"
                    try:
                        status, job = signed_request(
                            host,
                            port,
                            token,
                            {},
                            method="GET",
                            path=job_path,
                            body=b"",
                        )
                    except Exception as error:
                        failures += 1
                        print(f"[alive] job status request failed ({failures}): {error}", flush=True)
                        time.sleep(POLL_INTERVAL)
                        continue
                    if status != 200 or not job.get("ok"):
                        failures += 1
                        print(f"[alive] job status rejected ({failures}): {job}", flush=True)
                        time.sleep(POLL_INTERVAL)
                        continue

                    status_text = str(job.get("status") or "")
                    message = str(job.get("message") or "")
                    if (status_text, message) != last_phase:
                        print(f"[{status_text}] {message}", flush=True)
                        last_phase = (status_text, message)

                    if status_text == "succeeded":
                        print(
                            f"[phase] deployment succeeded with {recoveries} recovery attempt(s)",
                            flush=True,
                        )
                        return
                    if status_text == "failed":
                        raise DeploymentError(message or "Deployment failed")

                    if lock_during_deploy and not locked and is_install_stage(message):
                        print(
                            f"[phase] install stage reached ({message or status_text}); locking the display",
                            flush=True,
                        )
                        lock_device_display(device["identifier"])
                        locked = True

                    time.sleep(POLL_INTERVAL)

                last_status = last_phase[0] if last_phase else "unknown"
                last_message = last_phase[1] if last_phase else "none"
                raise DeploymentError(
                    f"Timed out after {timeout:g}s waiting for the SideStore deployment "
                    f"(last status={last_status}, message={last_message}, "
                    f"recoveries={recoveries}, liveness failures={failures})"
                )

            for attempt in range(2):
                try:
                    run_job()
                    return
                except DeploymentError as error:
                    if attempt == 0 and is_vpn_connection_error(str(error)):
                        enable_local_dev_vpn(device["identifier"])
                        time.sleep(10)
                        continue
                    raise
        finally:
            server.shutdown()
            server.server_close()
            if session["woke"]:
                try:
                    restore_display_off(device["identifier"])
                except Exception as error:
                    print(f"[phase] failed to restore the screen-off state: {error}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ipa", required=True, type=Path)
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--device-id", default=os.environ.get("NONAME_IOS_DEVICE_ID"))
    parser.add_argument("--host", default=os.environ.get("NONAME_IOS_HOST"))
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--keep-ipa", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        deploy(
            ipa_path=args.ipa.resolve(),
            device_id=args.device_id,
            bundle_id=args.bundle_id,
            host_override=args.host,
            timeout=args.timeout,
        )
        print("Deployment completed successfully.")
        return 0
    finally:
        if not args.keep_ipa:
            args.ipa.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
