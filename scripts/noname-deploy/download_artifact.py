#!/usr/bin/env python3
"""Download a GitHub Actions artifact using parallel HTTP ranges."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
CHUNK_SIZE = 8 * 1024 * 1024


class ArtifactError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_headers(token: str, *, range_header: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "noname-lan-deploy",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if range_header:
        headers["Range"] = range_header
    return headers


def read_json(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers=request_headers(token))
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def resolve_artifact(repository: str, run_id: int, name: str, token: str) -> dict[str, Any]:
    payload = read_json(
        f"{API_ROOT}/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100",
        token,
    )
    for artifact in payload.get("artifacts", []):
        if artifact.get("name") == name and not artifact.get("expired"):
            return artifact
    raise ArtifactError(f"Artifact {name!r} was not found in run {run_id}")


def resolve_signed_url(artifact_id: int, repository: str, token: str) -> str:
    request = urllib.request.Request(
        f"{API_ROOT}/repos/{repository}/actions/artifacts/{artifact_id}/zip",
        headers=request_headers(token),
    )
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            location = error.headers.get("Location")
            if location:
                return location
        raise ArtifactError(f"Artifact redirect failed with HTTP {error.code}") from error
    raise ArtifactError("Artifact endpoint did not return a redirect")


def download_part(
    *,
    artifact_id: int,
    repository: str,
    token: str,
    start: int,
    end: int,
    destination: Path,
) -> int:
    expected = end - start + 1
    last_error: Exception | None = None
    for attempt in range(8):
        try:
            signed_url = resolve_signed_url(artifact_id, repository, token)
            request = urllib.request.Request(
                signed_url,
                headers={"Range": f"bytes={start}-{end}", "User-Agent": "noname-lan-deploy"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                if response.status == 200:
                    raise ArtifactError("Artifact server ignored the requested byte range")
                if response.status != 206:
                    raise ArtifactError(f"Artifact range returned HTTP {response.status}")
                written = 0
                with destination.open("wb") as file:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        file.write(chunk)
                        written += len(chunk)
            if written != expected:
                raise ArtifactError(
                    f"Range {start}-{end} returned {written} bytes instead of {expected}"
                )
            return written
        except Exception as error:
            last_error = error
            destination.unlink(missing_ok=True)
            time.sleep(min(2**attempt, 20))
    raise ArtifactError(f"Range {start}-{end} failed after retries: {last_error}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("error: GH_TOKEN is required", file=sys.stderr)
        return 1

    output_dir = args.output_dir.resolve()
    if output_dir == Path(output_dir.anchor) or output_dir == Path.home():
        print(f"error: refusing to clear unsafe output directory: {output_dir}", file=sys.stderr)
        return 1
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    try:
        artifact = resolve_artifact(args.repository, args.run_id, args.name, token)
        artifact_id = int(artifact["id"])
        size = int(artifact["size_in_bytes"])
        if size <= 0:
            raise ArtifactError("Artifact is empty")

        part_dir = output_dir / ".parts"
        part_dir.mkdir()
        ranges = [
            (index, start, min(start + CHUNK_SIZE - 1, size - 1))
            for index, start in enumerate(range(0, size, CHUNK_SIZE))
        ]
        print(f"Downloading {size} bytes in {len(ranges)} ranges", file=sys.stderr, flush=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    download_part,
                    artifact_id=artifact_id,
                    repository=args.repository,
                    token=token,
                    start=start,
                    end=end,
                    destination=part_dir / f"{index:03d}.part",
                ): index
                for index, start, end in ranges
            }
            done = 0
            for future in concurrent.futures.as_completed(futures):
                future.result()
                done += 1
                if done == len(futures) or done % max(1, len(futures) // 10) == 0:
                    print(f"Downloaded {done}/{len(futures)} ranges", file=sys.stderr, flush=True)

        archive = output_dir / "artifact.zip"
        with archive.open("wb") as output:
            for index, _start, _end in ranges:
                with (part_dir / f"{index:03d}.part").open("rb") as part:
                    shutil.copyfileobj(part, output)
        if archive.stat().st_size != size:
            raise ArtifactError(
                f"Downloaded artifact size {archive.stat().st_size} does not match {size}"
            )

        extracted = output_dir / "extracted"
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)

        ipas = sorted(extracted.rglob("*.ipa"))
        if not ipas:
            raise ArtifactError("Artifact does not contain an IPA")
        preferred = [path for path in ipas if path.name == "noname-ios-unsigned.ipa"]
        ipa = preferred[0] if preferred else ipas[0]

        checksums = sorted(extracted.rglob("*.sha256"))
        if checksums:
            expected = checksums[0].read_text(encoding="utf-8").strip().split()[0].lower()
            actual = sha256_file(ipa)
            if actual != expected:
                raise ArtifactError(f"IPA SHA-256 mismatch: {actual} != {expected}")
            print(f"Verified SHA-256 {actual}", file=sys.stderr, flush=True)

        shutil.rmtree(part_dir)
        archive.unlink(missing_ok=True)
        print(ipa, flush=True)
        return 0
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
