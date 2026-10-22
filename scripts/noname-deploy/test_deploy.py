import hashlib
import hmac
import pathlib
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
