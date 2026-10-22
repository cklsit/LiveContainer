#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/cklsit/noname}"
RUNNER_NAME="${RUNNER_NAME:-noname-ios-$(hostname -s)}"
RUNNER_LABEL="${RUNNER_LABEL:-noname-ios-deploy}"
RUNNER_ROOT="${RUNNER_ROOT:-$HOME/actions-runner-noname}"
PYMOBILEDEVICE3_VERSION="${PYMOBILEDEVICE3_VERSION:-11.43.1}"

if [[ -z "${RUNNER_TOKEN:-}" ]]; then
  echo "RUNNER_TOKEN is required" >&2
  exit 1
fi

case "$(uname -m)" in
  aarch64|arm64) runner_arch=arm64 ;;
  x86_64|amd64) runner_arch=x64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "$RUNNER_ROOT"
cd "$RUNNER_ROOT"

latest="$(
  curl --http1.1 --retry 8 --retry-all-errors -fsSL \
    https://api.github.com/repos/actions/runner/releases/latest |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].removeprefix("v"))'
)"
archive="actions-runner-linux-${runner_arch}-${latest}.tar.gz"
download="https://github.com/actions/runner/releases/download/v${latest}/${archive}"

if [[ ! -f .runner ]]; then
  curl --http1.1 --retry 8 --retry-all-errors --connect-timeout 20 \
    -fL "$download" -o "$archive"
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
