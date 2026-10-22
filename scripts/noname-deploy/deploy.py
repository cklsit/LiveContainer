#!/usr/bin/env python3
"""Deploy an IPA through the authenticated Noname endpoint in built-in SideStore."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEPLOY_PORT = 49321
SERVICE_TYPE = "_noname-deploy._tcp"
SERVICE_DNS_TYPE = f"{SERVICE_TYPE}.local."
DEFAULT_BUNDLE_ID = "com.libnoname.noname"
HOST_BUNDLE_ID = "com.kdt.livecontainer"
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DeploymentError(RuntimeError):
    pass


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
        display_command = " ".join(command)
        stdout = result.stdout
        stderr = result.stderr
        for value in redact:
            display_command = display_command.replace(value, "<redacted>")
            stdout = stdout.replace(value, "<redacted>")
            stderr = stderr.replace(value, "<redacted>")
        raise DeploymentError(
            f"Command failed ({result.returncode}): {display_command}\n"
            f"{stdout}\n{stderr}"
        )
    return result


def pymobiledevice3_command() -> list[str]:
    configured = os.environ.get("PYMOBILEDEVICE3")
    executable = configured or shutil.which("pymobiledevice3")
    if executable:
        return [executable]
    if importlib.util.find_spec("pymobiledevice3") is not None:
        return [sys.executable, "-m", "pymobiledevice3"]
    raise DeploymentError(
        "pymobiledevice3 was not found. Install it in the runner environment "
        "or set PYMOBILEDEVICE3 to its executable."
    )


def parse_pymobiledevice3_devices(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise DeploymentError("pymobiledevice3 discovery returned an unexpected payload")
    candidates = []
    for device in payload:
        if not isinstance(device, dict):
            continue
        product_type = str(device.get("ProductType") or "")
        device_class = str(device.get("DeviceClass") or "")
        identifier = str(device.get("UniqueDeviceID") or "")
        if not identifier:
            continue
        if device_class != "iPad" and not product_type.startswith("iPad"):
            continue
        candidates.append(
            {
                "identifier": identifier,
                "name": str(device.get("DeviceName") or product_type or identifier),
                "host": device.get("ip"),
            }
        )
    return candidates


def parse_remote_pairing_devices(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise DeploymentError("pymobiledevice3 RemotePairing discovery returned an unexpected payload")
    devices = payload.get("wifi", [])
    if not isinstance(devices, list):
        raise DeploymentError("pymobiledevice3 RemotePairing discovery did not return a Wi-Fi device list")

    candidates = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        identifier = str(device.get("identifier") or "")
        if not identifier:
            continue
        candidates.append(
            {
                "identifier": identifier,
                "name": f"RemotePairing iPad {identifier[-8:]}",
                "host": device.get("address"),
            }
        )
    return candidates


def select_pymobiledevice3_device() -> dict[str, Any]:
    executable = pymobiledevice3_command()
    result = run(
        [*executable, "--no-color", "bonjour", "mobdev2", "--timeout", "5"],
        timeout=30,
    )
    try:
        candidates = parse_pymobiledevice3_devices(json.loads(result.stdout))
    except json.JSONDecodeError as error:
        raise DeploymentError(f"Could not parse pymobiledevice3 discovery output: {result.stdout}") from error

    if not candidates:
        remote_result = run(
            [*executable, "--no-color", "remote", "browse", "--timeout", "8"],
            timeout=30,
        )
        try:
            candidates = parse_remote_pairing_devices(json.loads(remote_result.stdout))
        except json.JSONDecodeError as error:
            raise DeploymentError(
                f"Could not parse pymobiledevice3 RemotePairing discovery output: {remote_result.stdout}"
            ) from error

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


def launch_configured_side_store(device_id: str, token: str) -> None:
    if sys.platform == "darwin":
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
    environment["NONAME_DEPLOY_TOKEN"] = token
    helper = Path(__file__).with_name("launch_ios_app.py")
    run(
        [
            sys.executable,
            str(helper),
            "--udid",
            device_id,
            "--bundle-id",
            HOST_BUNDLE_ID,
            "--token-env",
            "NONAME_DEPLOY_TOKEN",
        ],
        timeout=120,
        env=environment,
        redact=(token,),
    )


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


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

    device = select_device(device_id)
    token = secrets.token_urlsafe(32)
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

    try:
        ipa_url = f"http://{runner_ip}:{server.server_address[1]}{served_path}"
        payload = {
            "bundleID": bundle_id,
            "ipaURL": ipa_url,
            "sha256": digest,
        }
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

        print(f"Deployment job: {job_id}")
        deadline = time.monotonic() + timeout
        job_path = f"/v1/jobs/{job_id}"
        while time.monotonic() < deadline:
            status, job = signed_request(
                host,
                port,
                token,
                {},
                method="GET",
                path=job_path,
                body=b"",
            )
            if status != 200 or not job.get("ok"):
                raise DeploymentError(f"Deployment status request was rejected: {job}")
            status_text = job.get("status")
            message = job.get("message") or ""
            print(f"[{status_text}] {message}", flush=True)
            if status_text == "succeeded":
                return
            if status_text == "failed":
                raise DeploymentError(message or "Deployment failed")
            time.sleep(2)
        raise DeploymentError("Timed out waiting for SideStore deployment")
    finally:
        server.shutdown()
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
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
