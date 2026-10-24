import hashlib
import hmac
import pathlib
import plistlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import deploy
import passcode


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
        self.assertTrue(
            deploy.is_vpn_connection_error(
                "SideStore is unable to reach the device endpoint: "
                "VPN tunnel iface is up but tunnel peer IP is not yet reachable "
                "Cause: DeviceEndpointNotInitialized"
            )
        )
        self.assertFalse(deploy.is_vpn_connection_error("code signature is invalid"))

    def test_passcode_digits_map_to_hid_usages(self):
        self.assertEqual(
            passcode.passcode_usages("123456"),
            [0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23],
        )
        with self.assertRaises(ValueError):
            passcode.passcode_usages("12ab")


class InstallStageTests(unittest.TestCase):
    def test_messages_before_the_install_stage_are_ignored(self):
        for message in (
            "",
            "starting",
            "starting SideStore database",
            "downloading IPA",
            "verifying IPA",
            "uninstalling existing app",
        ):
            self.assertFalse(deploy.is_install_stage(message), message)

    def test_install_and_success_messages_are_detected(self):
        for message in (
            "installing IPA",
            "Installing IPA",
            "INSTALLING IPA now",
            "succeeded",
            "deployment succeeded",
        ):
            self.assertTrue(deploy.is_install_stage(message), message)


class ShouldRecoverTests(unittest.TestCase):
    def test_liveness_must_fail(self):
        self.assertFalse(
            deploy.should_recover(
                alive=True,
                failures=9,
                locked=True,
                ever_alive=True,
                recoveries=0,
                max_recoveries=2,
            )
        )

    def test_failure_threshold_is_inclusive(self):
        self.assertFalse(
            deploy.should_recover(
                alive=False,
                failures=2,
                locked=True,
                ever_alive=True,
                recoveries=0,
                max_recoveries=2,
            )
        )
        self.assertTrue(
            deploy.should_recover(
                alive=False,
                failures=3,
                locked=True,
                ever_alive=True,
                recoveries=0,
                max_recoveries=2,
            )
        )

    def test_never_locked_and_never_seen_alive_is_not_recovered(self):
        self.assertFalse(
            deploy.should_recover(
                alive=False,
                failures=5,
                locked=False,
                ever_alive=False,
                recoveries=0,
                max_recoveries=2,
            )
        )

    def test_alive_once_without_manual_lock_is_recovered(self):
        # iOS auto-lock may suspend the host before the runner locks the display.
        self.assertTrue(
            deploy.should_recover(
                alive=False,
                failures=3,
                locked=False,
                ever_alive=True,
                recoveries=0,
                max_recoveries=2,
            )
        )

    def test_recovery_budget_is_enforced(self):
        self.assertFalse(
            deploy.should_recover(
                alive=False,
                failures=5,
                locked=True,
                ever_alive=True,
                recoveries=2,
                max_recoveries=2,
            )
        )
        self.assertFalse(
            deploy.should_recover(
                alive=False,
                failures=5,
                locked=True,
                ever_alive=True,
                recoveries=0,
                max_recoveries=0,
            )
        )
        self.assertTrue(
            deploy.should_recover(
                alive=False,
                failures=5,
                locked=True,
                ever_alive=True,
                recoveries=1,
                max_recoveries=2,
            )
        )


class PowerStateTests(unittest.TestCase):
    def test_missing_telemetry_is_reported_as_unknown(self):
        rendered = deploy.describe_power_state({})
        self.assertIn("backgroundActive=unknown", rendered)
        self.assertIn("audioActive=unknown", rendered)

    def test_boolean_telemetry_is_rendered(self):
        rendered = deploy.describe_power_state({"backgroundActive": True, "audioActive": False})
        self.assertIn("backgroundActive=true", rendered)
        self.assertIn("audioActive=false", rendered)

    def test_probe_alive_treats_transport_errors_as_dead(self):
        with mock.patch.object(deploy.LOCAL_OPENER, "open", side_effect=OSError("connection refused")):
            self.assertFalse(deploy.probe_alive("192.0.2.10", 49321, timeout=0.1))


class UniqueBuildVersionTests(unittest.TestCase):
    def test_pinned_project_version_is_not_unique(self):
        for version in ("1", "1.0", " 1 ", "2", "7", None, "", "   "):
            self.assertFalse(deploy.is_unique_build_version(version), version)

    def test_short_versions_are_not_unique(self):
        self.assertFalse(deploy.is_unique_build_version("1234567"))

    def test_per_build_version_is_unique(self):
        for version in ("12345678", "20250101.7", "1.0.1759300000"):
            self.assertTrue(deploy.is_unique_build_version(version), version)


class ShouldRetryFailureTests(unittest.TestCase):
    def test_locked_failure_with_budget_is_retried(self):
        self.assertTrue(deploy.should_retry_failure("failed", True, 0, 2))
        self.assertTrue(deploy.should_retry_failure("failed", True, 1, 2))

    def test_only_failed_statuses_are_retried(self):
        for status in ("", "queued", "running", "succeeded", "error"):
            self.assertFalse(deploy.should_retry_failure(status, True, 0, 2), status)
        self.assertTrue(deploy.should_retry_failure("FAILED", True, 0, 2))

    def test_unlocked_failure_is_reported_immediately(self):
        self.assertFalse(deploy.should_retry_failure("failed", False, 0, 2))

    def test_retry_respects_the_recovery_budget(self):
        self.assertFalse(deploy.should_retry_failure("failed", True, 2, 2))
        self.assertFalse(deploy.should_retry_failure("failed", True, 0, 0))


class IPABuildVersionTests(unittest.TestCase):
    @staticmethod
    def write_ipa(directory: str, version: str) -> pathlib.Path:
        path = pathlib.Path(directory) / "Test.ipa"
        info = plistlib.dumps(
            {
                "CFBundleIdentifier": "com.libnoname.noname",
                "CFBundleVersion": version,
            }
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Payload/Test.app/Info.plist", info)
            archive.writestr("Payload/Test.app/Test", b"binary")
        return path

    def test_reads_cf_bundle_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_ipa(directory, "20250101.7")
            self.assertEqual(deploy.ipa_build_version(path), "20250101.7")

    def test_archive_without_payload_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "broken.ipa"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("readme.txt", b"no payload here")
            self.assertIsNone(deploy.ipa_build_version(path))

    def test_missing_and_non_zip_files_return_none(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "missing.ipa"
            self.assertIsNone(deploy.ipa_build_version(missing))
            garbage = pathlib.Path(directory) / "garbage.ipa"
            garbage.write_bytes(b"not a zip archive")
            self.assertIsNone(deploy.ipa_build_version(garbage))


class ScreenRestoreTests(unittest.TestCase):
    def test_screen_off_before_and_on_after_is_restored(self):
        self.assertTrue(deploy.should_restore_screen_off(True, False))

    def test_already_locked_is_left_alone(self):
        self.assertFalse(deploy.should_restore_screen_off(True, True))

    def test_screen_on_before_is_never_forced_off(self):
        self.assertFalse(deploy.should_restore_screen_off(False, False))
        self.assertFalse(deploy.should_restore_screen_off(False, True))


if __name__ == "__main__":
    unittest.main()
