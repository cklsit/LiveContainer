#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/cklsit/noname}"
RUNNER_NAME="${RUNNER_NAME:-noname-ios-$(hostname -s)}"
RUNNER_LABEL="${RUNNER_LABEL:-noname-ios-deploy}"
RUNNER_ROOT="${RUNNER_ROOT:-$HOME/actions-runner-noname}"
RUNNER_VERSION="${RUNNER_VERSION:-2.338.1}"
PYMOBILEDEVICE3_VERSION="${PYMOBILEDEVICE3_VERSION:-11.43.1}"

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

archive="actions-runner-linux-${runner_arch}-${RUNNER_VERSION}.tar.gz"
download="${RUNNER_DOWNLOAD_URL:-https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${archive}}"

if [[ ! -f .runner ]]; then
  if [[ -f "$archive" ]]; then
    curl --http1.1 -C - --retry 8 --retry-all-errors --connect-timeout 20 \
      -fL "$download" -o "$archive" || true
  else
    curl --http1.1 --retry 8 --retry-all-errors --connect-timeout 20 \
      -fL "$download" -o "$archive"
  fi

  if ! echo "${runner_sha256}  ${archive}" | sha256sum -c -; then
    rm -f "$archive"
    curl --http1.1 --retry 8 --retry-all-errors --connect-timeout 20 \
      -fL "$download" -o "$archive"
    echo "${runner_sha256}  ${archive}" | sha256sum -c -
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

python3 -m venv "$RUNNER_ROOT/.pmd3"
"$RUNNER_ROOT/.pmd3/bin/python" -m pip install --disable-pip-version-check \
  "pymobiledevice3==${PYMOBILEDEVICE3_VERSION}"

if [[ -f "$RUNNER_ROOT/runner.pid" ]]; then
  old_pid="$(cat "$RUNNER_ROOT/runner.pid")"
  if kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid"
    sleep 2
  fi
fi

nohup env \
  NONAME_DEPLOY_PYTHON="$RUNNER_ROOT/.pmd3/bin/python" \
  PYMOBILEDEVICE3="$RUNNER_ROOT/.pmd3/bin/pymobiledevice3" \
  PYMOBILEDEVICE3_RECONNECT=1 \
  "$RUNNER_ROOT/run.sh" >"$RUNNER_ROOT/runner.log" 2>&1 &
echo $! >"$RUNNER_ROOT/runner.pid"

echo "Runner started as PID $(cat "$RUNNER_ROOT/runner.pid")."
echo "Log: $RUNNER_ROOT/runner.log"
