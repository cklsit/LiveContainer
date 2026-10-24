#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="${REPO_URL:-https://github.com/cklsit/noname}"
RUNNER_NAME="${RUNNER_NAME:-noname-ios-$(hostname -s)}"
RUNNER_LABEL="${RUNNER_LABEL:-noname-ios-deploy}"
RUNNER_ROOT="${RUNNER_ROOT:-$HOME/actions-runner-noname}"
RUNNER_VERSION="${RUNNER_VERSION:-2.338.1}"
PYMOBILEDEVICE3_VERSION="${PYMOBILEDEVICE3_VERSION:-11.43.1}"
KEEP_IPAD_AWAKE="${KEEP_IPAD_AWAKE:-0}"

if [[ -z "${RUNNER_TOKEN:-}" ]]; then
  echo "RUNNER_TOKEN is required" >&2
  exit 1
fi

case "$(uname -m)" in
  aarch64|arm64)
    runner_arch=arm64
    runner_sha256="${RUNNER_SHA256:-475e0c149fd3b611610127b9664ba5bacf5c6928795cc448ab520bb5f324bede}"
    ;;
  x86_64|amd64)
    runner_arch=x64
    runner_sha256="${RUNNER_SHA256:-08e9ccb3604d2462c24b5e33604dc30810296ca15cc65f23cffd4317eb144c37}"
    ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "$RUNNER_ROOT"
cd "$RUNNER_ROOT"

if ! command -v ldd >/dev/null 2>&1; then
  mkdir -p "$RUNNER_ROOT/.tools"
  cp "$SCRIPT_DIR/ldd-shim.sh" "$RUNNER_ROOT/.tools/ldd"
  chmod +x "$RUNNER_ROOT/.tools/ldd"
  export PATH="$RUNNER_ROOT/.tools:$PATH"
fi

if ! command -v ldconfig >/dev/null 2>&1 && [[ ! -x /sbin/ldconfig ]]; then
  mkdir -p "$RUNNER_ROOT/.tools"
  cp "$SCRIPT_DIR/ldconfig-shim.sh" "$RUNNER_ROOT/.tools/ldconfig"
  chmod +x "$RUNNER_ROOT/.tools/ldconfig"
  export PATH="$RUNNER_ROOT/.tools:$PATH"
fi

archive="actions-runner-linux-${runner_arch}-${RUNNER_VERSION}.tar.gz"
download="${RUNNER_DOWNLOAD_URL:-https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${archive}}"

if [[ ! -f .runner ]]; then
  archive_ok() {
    [[ -f "$archive" ]] && echo "${runner_sha256}  ${archive}" | sha256sum -c - >/dev/null 2>&1
  }

  if ! archive_ok; then
    if [[ -f "$archive" ]]; then
      curl --http1.1 -C - --retry 8 --retry-all-errors --connect-timeout 20 \
        -fL "$download" -o "$archive" || true
    else
      curl --http1.1 --retry 8 --retry-all-errors --connect-timeout 20 \
        -fL "$download" -o "$archive"
    fi
  fi

  if ! archive_ok; then
    rm -f "$archive"
    curl --http1.1 --retry 8 --retry-all-errors --connect-timeout 20 \
      -fL "$download" -o "$archive"
    archive_ok
  fi

  tar -xzf "$archive"
  rm -f "$archive"

  ./config.sh \
    --unattended \
    --replace \
    --url "$REPO_URL" \
    --token "$RUNNER_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABEL"
fi

if python3 -m venv "$RUNNER_ROOT/.pmd3"; then
  deploy_python="$RUNNER_ROOT/.pmd3/bin/python"
  deploy_pythonpath=""
  "$deploy_python" -m pip install --disable-pip-version-check \
    "pymobiledevice3==${PYMOBILEDEVICE3_VERSION}"
else
  rm -rf "$RUNNER_ROOT/.pmd3"
  package_root="$RUNNER_ROOT/.python-packages"
  deploy_python="$(command -v python3)"
  deploy_pythonpath="$package_root"
  mkdir -p "$package_root"
  "$deploy_python" -m pip install --disable-pip-version-check --target "$package_root" \
    --no-deps "pymobiledevice3==${PYMOBILEDEVICE3_VERSION}"
  "$deploy_python" -m pip install --disable-pip-version-check --target "$package_root" \
    -r "$SCRIPT_DIR/requirements-noname-deploy.txt"
fi

{
  printf 'export NONAME_DEPLOY_PYTHON=%q\n' "$deploy_python"
  printf 'export PYMOBILEDEVICE3_RECONNECT=1\n'
  if [[ -n "$deploy_pythonpath" ]]; then
    printf 'export PYTHONPATH=%q\n' "$deploy_pythonpath"
  fi
} >"$RUNNER_ROOT/runner.env"

mkdir -p "$RUNNER_ROOT/noname-deploy"
cp \
  "$SCRIPT_DIR/discover_ios_devices.py" \
  "$SCRIPT_DIR/keep_device_awake.py" \
  "$SCRIPT_DIR/launch_ios_app.py" \
  "$SCRIPT_DIR/passcode.py" \
  "$SCRIPT_DIR/pair_host.py" \
  "$RUNNER_ROOT/noname-deploy/"

if [[ -f "$RUNNER_ROOT/runner.pid" ]]; then
  old_pid="$(cat "$RUNNER_ROOT/runner.pid")"
  if kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid"
    sleep 2
  fi
fi

set -a
# shellcheck disable=SC1091
source "$RUNNER_ROOT/runner.env"
set +a
nohup "$RUNNER_ROOT/run.sh" >"$RUNNER_ROOT/runner.log" 2>&1 &
echo $! >"$RUNNER_ROOT/runner.pid"

if [[ "$KEEP_IPAD_AWAKE" == "1" ]]; then
  if [[ -f "$RUNNER_ROOT/keep-awake.pid" ]]; then
    old_keep_awake_pid="$(cat "$RUNNER_ROOT/keep-awake.pid")"
    if kill -0 "$old_keep_awake_pid" 2>/dev/null; then
      kill "$old_keep_awake_pid"
      sleep 1
    fi
  fi
  nohup "$NONAME_DEPLOY_PYTHON" "$RUNNER_ROOT/noname-deploy/keep_device_awake.py" \
    >"$RUNNER_ROOT/keep-awake.log" 2>&1 &
  echo $! >"$RUNNER_ROOT/keep-awake.pid"
fi

echo "Runner started as PID $(cat "$RUNNER_ROOT/runner.pid")."
echo "Log: $RUNNER_ROOT/runner.log"
