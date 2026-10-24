import hashlib
import hmac
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import deploy
import launch_ios_app


class CanonicalRequestTests(unittest.TestCase):
    def test_body_digest_and_signature_are_stable(self):
        body = b'{"bundleID":"com.libnoname.noname"}'
        canonical = deploy.canonical_request(
            "POST",
            "/v1/deploy",
            "1700000000",
            "0123456789abcdef",
            body,
        )
        expected = "\n".join(
            (
                "POST",
                "/v1/deploy",
                "1700000000",
                "0123456789abcdef",
                hashlib.sha256(body).hexdigest(),
            )
        ).encode()
        self.assertEqual(canonical, expected)
        self.assertEqual(
            hmac.new(b"secret", canonical, hashlib.sha256).hexdigest(),
            hmac.new(b"secret", expected, hashlib.sha256).hexdigest(),
        )

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "empty.ipa"
            path.write_bytes(b"")
            self.assertEqual(
                deploy.sha256_file(path),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )

    def test_pymobiledevice3_discovery_keeps_only_ipads(self):
        payload = {
            "devices": [
                {
                    "identifier": "ipad-udid",
                    "name": "Test iPad",
                    "deviceClass": "iPad",
                    "productType": "iPad14,8",
                    "host": "192.0.2.10",
                },
                {
                    "identifier": "iphone-udid",
                    "name": "Test iPhone",
                    "deviceClass": "iPhone",
                    "productType": "iPhone17,1",
                    "host": "192.0.2.11",
                },
            ]
        }
        self.assertEqual(
            deploy.parse_discovered_devices(payload),
            [
                {
                    "identifier": "ipad-udid",
                    "name": "Test iPad",
                    "host": "192.0.2.10",
                }
            ],
        )

    def test_remote_pairing_discovery_uses_wifi_devices(self):
        payload = {
            "devices": [
                {
                    "identifier": "00008112-000000000000001E",
                    "name": "RemotePairing iPad 0A47A01E",
                    "deviceClass": "iPad",
                    "productType": "",
                    "host": "192.0.2.10",
                    "transport": "remotepairing",
                }
            ]
        }
        self.assertEqual(
            deploy.parse_discovered_devices(payload),
            [
                {
                    "identifier": "00008112-000000000000001E",
                    "name": "RemotePairing iPad 0A47A01E",
                    "host": "192.0.2.10",
                }
            ],
        )

    def test_command_errors_redact_secrets(self):
        secret = "sensitive-launch-token"
        completed = deploy.subprocess.CompletedProcess(
            args=["command"],
            returncode=1,
            stdout=f"prefix {secret} suffix",
            stderr=f"failure {secret}",
        )
        with mock.patch.object(deploy.subprocess, "run", return_value=completed):
            with self.assertRaises(deploy.DeploymentError) as raised:
                deploy.run(["command", secret], redact=(secret,))
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("<redacted>", str(raised.exception))

    def test_run_with_retries_recovers_from_transient_tunnel_failure(self):
        transient = deploy.subprocess.CompletedProcess(
            args=["command"],
            returncode=1,
            stdout="",
            stderr="No RemotePairing tunnel is available",
        )
        success = deploy.subprocess.CompletedProcess(
            args=["command"],
            returncode=0,
            stdout='{"ok":true}',
            stderr="",
        )
        with (
            mock.patch.object(
                deploy.subprocess,
                "run",
                side_effect=[transient, success],
            ) as run_mock,
            mock.patch.object(deploy.time, "sleep"),
        ):
            result = deploy.run_with_retries(["command"], attempts=2)
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(result.returncode, 0)

    def test_vpn_connection_errors_are_recognized(self):
        self.assertTrue(
            deploy.is_vpn_connection_error(
                "VPN Connection Error: No utun interface detected - LocalDevVPN is not connected"
            )
        )
        self.assertFalse(deploy.is_vpn_connection_error("code signature is invalid"))

    def test_passcode_digits_map_to_hid_usages(self):
        self.assertEqual(
            launch_ios_app.passcode_usages("123456"),
            [0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23],
        )
        with self.assertRaises(launch_ios_app.LaunchError):
            launch_ios_app.passcode_usages("12ab")


if __name__ == "__main__":
    unittest.main()
