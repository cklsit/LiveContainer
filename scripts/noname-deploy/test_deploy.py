import hashlib
import hmac
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import deploy


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
        payload = [
            {
                "UniqueDeviceID": "ipad-udid",
                "DeviceName": "Test iPad",
                "DeviceClass": "iPad",
                "ProductType": "iPad14,8",
                "ip": "192.0.2.10",
            },
            {
                "UniqueDeviceID": "iphone-udid",
                "DeviceName": "Test iPhone",
                "DeviceClass": "iPhone",
                "ProductType": "iPhone17,1",
                "ip": "192.0.2.11",
            },
        ]
        self.assertEqual(
            deploy.parse_pymobiledevice3_devices(payload),
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
            "usb": [],
            "wifi": [
                {
                    "identifier": "00008112-000E61640A47A01E",
                    "address": "192.0.2.10",
                    "port": 49152,
                }
            ],
        }
        self.assertEqual(
            deploy.parse_remote_pairing_devices(payload),
            [
                {
                    "identifier": "00008112-000E61640A47A01E",
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


if __name__ == "__main__":
    unittest.main()
