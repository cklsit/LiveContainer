# Noname LAN deployment

This branch adds an opt-in deployment endpoint to the LiveContainer-specific
SideStore build. A self-hosted Linux or macOS runner can uninstall and
reinstall one allowlisted app on a paired iPad over the local network.

## Security model

- The endpoint is disabled until SideStore receives a per-run secret.
- The runner passes that secret through an authenticated launch mechanism.
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
- Python 3 and `pymobiledevice3`.
- A Linux runner with RemotePairing and RemoteXPC access, or a macOS runner
  with Xcode `devicectl`.
- LocalDevVPN installed and already authorized for its system VPN profile.
- Exactly one paired iPad visible, or an explicit `--device-id`.

The initial installation of LiveContainer+SideStore still requires the normal
SideStore/AltStore flow. This automation covers later Noname deployments.

Create a repository runner registration token on the local machine:

```bash
gh api -X POST repos/cklsit/noname/actions/runners/registration-token --jq .token
```

Then run the provided installer on the LAN Linux host:

```bash
export RUNNER_ROOT=/volume1/docker/github-runner-noname
RUNNER_TOKEN="<short-lived-token>" \
  bash scripts/noname-deploy/install-runner-linux.sh
```

The registration token is short-lived. Do not commit it or place it in workflow
YAML. If the host Python lacks `venv`, install its `pip` package first; the
installer then uses an isolated `--target` package directory.

For iOS 27 and newer, create the one-time RemotePairing trust relationship with
the same user that owns the runner:

```bash
source "$RUNNER_ROOT/runner.env"
"$NONAME_DEPLOY_PYTHON" "$RUNNER_ROOT/noname-deploy/pair_host.py" \
  --name Synology-Noname --timeout 300
```

On the iPad, enable Developer Mode, open **Settings > Developer > Paired Macs**,
select the advertised host, and enter the displayed six-digit code once. The
runner reuses that pairing record for later unattended builds.

For older iOS versions, use the corresponding `pymobiledevice3 remote pair`
flow and retain its pairing record outside the repository.

## Build modified SideStore

Run the `Xcode - Build` workflow with:

```text
build_sidestore_from_source: true
sidestore_source_repository: cklsit/SideStore
sidestore_source_ref: feat/noname-auto-deploy
```

The resulting `LiveContainer+SideStore.ipa` contains the deployment endpoint.
Alternatively, pass a prebuilt modified IPA through `sidestore_ipa_url`.
Install links containing `auto=1` start the existing SideStore installation
pipeline without presenting the install confirmation dialog. Links without that
flag retain the normal confirmation flow.

## Deploy an IPA

On the LAN self-hosted runner:

```bash
python3 scripts/noname-deploy/deploy.py \
  --ipa /absolute/path/to/noname-ios-unsigned.ipa
```

Optional inputs:

```text
--device-id <udid>          Select a specific paired iPad
--host <lan-host>           Skip Bonjour discovery
--timeout <seconds>         Deployment timeout; default 900
--keep-ipa                  Keep the source IPA after the run
```

For passcode-protected devices, set `NONAME_IOS_PASSCODE` in the runner
environment or GitHub Actions secrets. The value is passed only through the
process environment and is never printed. SideStore starts a silent-audio
background task before accepting the deployment job; the runner then locks the
display and continues polling while the screen is off. Set
`NONAME_LOCK_DURING_DEPLOY=0` only when the display must stay on.

Deployment state machine, so the screen can stay off without losing the job:

1. `keep_device_awake.py` runs as a background process for the whole deployment
   and renews a `PreventUserIdleSystemSleep` assertion (120-second windows, at
   least 30 seconds apart), so the iPad does not idle into sleep while SideStore
   downloads and verifies the IPA.
2. The display is locked only once the job message reaches the install stage
   (`installing IPA`). Before that the screen stays on, which is where the
   download and code-signing handshake need the host app awake.
3. `GET /v1/hello` (no authentication) is polled every two seconds as a
   liveness probe. While it answers, the job is polled normally.
4. When it stops answering for three consecutive probes after the lock, the
   runner wakes the iPad, unlocks it (passcode from `NONAME_IOS_PASSCODE`),
   relaunches LiveContainer+SideStore with a fresh token, waits for `/v1/hello`
   again and resubmits the deployment as a new job.
5. If the interrupted job had already reached the install stage, the runner
   compares the installed `CFBundleVersion` with the IPA's before reinstalling.
   An identical build means the install finished just before the suspension and
   no work is redone.
6. When a recovery happened, one more lock-button press restores the screen-off
   state after the run.

Environment variables:

```text
NONAME_LOCK_DURING_DEPLOY  1 (default) locks the display at the install stage
NONAME_MAX_RECOVERIES      Automatic wake/unlock/restart cycles; default 2
NONAME_KEEP_AWAKE          1 (default) runs the assertion renewer in the background
NONAME_IOS_PASSCODE        Lock-screen passcode, environment only, never printed
NONAME_ALIVE_TIMEOUT       /v1/hello probe timeout in seconds; default 2.0
```

Newer SideStore builds also report `backgroundActive` and `audioActive` in
`/v1/hello`. The runner logs both as `[alive] SideStore answered /v1/hello
(backgroundActive=... audioActive=...)`. Older builds omit the fields; they are
then logged as `unknown` and no error is raised.

The source IPA is deleted after the deployment attempt unless `--keep-ipa` is
set. The runner also stops its temporary HTTP server on every exit path.
For Actions workflows, the reusable action can download the artifact directly
on the LAN runner with parallel byte ranges. This avoids the slow default
artifact download path for large IPA files.
Before relaunching LiveContainer, the runner wakes the iPad and launches
LocalDevVPN normally, allowing its `Auto-connect on Launch` setting to establish
the tunnel without a second toggle. If SideStore reports that no `utun`
interface exists, the runner invokes `localdevvpn://enable` once and retries the
deployment. SideStore's uninstall and signing operations rely on that
connection.
The Linux installer does not start a perpetual keep-awake daemon by default.
Set `KEEP_IPAD_AWAKE=1` only when a dedicated always-on policy is desired. The
deployment itself starts and stops `keep_device_awake.py` for its own duration
and never traverses home-screen accessibility focus in the background.

## Reusable action

```yaml
- uses: cklsit/LiveContainer/.github/actions/noname-ios-deploy@<commit-sha>
  with:
    ipa-path: ${{ github.workspace }}/noname-ios-unsigned.ipa
    device-id: ${{ vars.NONAME_IOS_DEVICE_ID }}
```

Pin the action to a full commit SHA. Run it only on a trusted self-hosted
runner, not on untrusted pull request code.
Developer inspection commands are rejected when the helper runs from the
production deployment action.

## Current limitations

- Device launch requires an existing RemotePairing trust relationship.
- A passcode-protected lock screen can be unlocked through the authenticated
  HID keyboard when `NONAME_IOS_PASSCODE` is configured. Without a passcode the
  recovery path stops with an explicit error instead of retrying blindly.
- iOS can still suspend the built-in SideStore host. Silent-audio keep-alive is
  the first line of defence: the assertion renewer keeps the device awake until
  the install stage, and the audio session is expected to keep the host alive
  afterwards. When iOS suspends it anyway, the liveness probe notices within
  about six seconds and the runner wakes, unlocks and restarts the host, up to
  `NONAME_MAX_RECOVERIES` times (default 2). Recovery is attempted once the
  display was locked by the runner **or** `/v1/hello` answered at least once, so
  an iOS auto-lock that happens before the install stage is recovered too.
  A host that never answered at all is not recovered, because that points at the
  tunnel or LocalDevVPN instead of a suspension.
- Recovery assumes the silent-audio task survives long enough to finish an
  install that is already running. If iOS suspends the host mid-install and the
  restart re-enters the job, the build is reinstalled from scratch.
- Apple Account, certificate, pairing, or LocalDevVPN failure stops deployment.
- Uninstalling the app deletes its local containers and data.
