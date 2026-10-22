# Noname LAN deployment

This branch adds an opt-in deployment endpoint to the LiveContainer-specific
SideStore build. A self-hosted macOS runner can uninstall and reinstall one
allowlisted app on a paired iPad over the local network.

## Security model

- The endpoint is disabled until SideStore receives a per-run secret.
- The runner passes that secret through `sidestore://noname-deploy/configure`.
- Requests use HMAC-SHA256, a five-minute timestamp window, and a one-time nonce.
- Only `com.libnoname.noname` is accepted.
- The iPad downloads the IPA over HTTP or HTTPS and verifies SHA-256 before
  uninstalling the current app.
- No Apple Account credential, pairing file, or deployment token belongs in the
  repository or workflow logs.

## Requirements

- LiveContainer+SideStore built from the matching SideStore source branch.
- SideStore signed in with an active signing certificate.
- A valid pairing file and LocalDevVPN connection.
- iPad unlocked and connected to the same network as the runner.
- A macOS runner with Xcode `devicectl`, `dns-sd`, and Python 3.
- Exactly one paired iPad visible, or an explicit `--device-id`.

The initial installation of LiveContainer+SideStore still requires the normal
SideStore/AltStore flow. This automation covers later Noname deployments.

## Build modified SideStore

Run the `Xcode - Build` workflow with:

```text
build_sidestore_from_source: true
sidestore_source_repository: cklsit/SideStore
sidestore_source_ref: feat/noname-auto-deploy
```

The resulting `LiveContainer+SideStore.ipa` contains the deployment endpoint.
Alternatively, pass a prebuilt modified IPA through `sidestore_ipa_url`.

## Deploy an IPA

On the LAN self-hosted runner:

```bash
python3 scripts/noname-deploy/deploy.py \
  --ipa /absolute/path/to/noname-ios-unsigned.ipa
```

Optional inputs:

```text
--device-id <devicectl-id>  Select a specific paired iPad
--host <lan-host>           Skip Bonjour discovery
--timeout <seconds>         Deployment timeout; default 900
--keep-ipa                  Keep the source IPA after the run
```

The source IPA is deleted after the deployment attempt unless `--keep-ipa` is
set. The runner also stops its temporary HTTP server on every exit path.

## Reusable action

```yaml
- uses: cklsit/LiveContainer/.github/actions/noname-ios-deploy@<commit-sha>
  with:
    ipa-path: ${{ github.workspace }}/noname-ios-unsigned.ipa
    device-id: ${{ vars.NONAME_IOS_DEVICE_ID }}
```

Pin the action to a full commit SHA. Run it only on a trusted self-hosted
runner, not on untrusted pull request code.

## Current limitations

- `devicectl` requires an existing trust relationship and an unlocked iPad.
- iOS may suspend background execution; keep LocalDevVPN/SideStore healthy and
  allow the runner to relaunch SideStore through `devicectl`.
- Apple Account, certificate, pairing, or LocalDevVPN failure stops deployment.
- Uninstalling the app deletes its local containers and data.
