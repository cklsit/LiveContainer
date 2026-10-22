#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/cklsit/noname}"
RUNNER_NAME="${RUNNER_NAME:-noname-ios-$(hostname -s)}"
RUNNER_LABEL="${RUNNER_LABEL:-noname-ios-deploy}"
RUNNER_ROOT="${RUNNER_ROOT:-$HOME/actions-runner-noname}"

if [[ -z "${RUNNER_TOKEN:-}" ]]; then
  echo "RUNNER_TOKEN is required" >&2
  exit 1
fi

case "$(uname -m)" in
  arm64) runner_arch=arm64 ;;
  x86_64) runner_arch=x64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

latest="$(
  curl -fsSL https://api.github.com/repos/actions/runner/releases/latest |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].removeprefix("v"))'
)"
archive="actions-runner-osx-${runner_arch}-${latest}.tar.gz"
download="https://github.com/actions/runner/releases/download/v${latest}/${archive}"

mkdir -p "$RUNNER_ROOT"
cd "$RUNNER_ROOT"
curl -fL "$download" -o "$archive"
tar -xzf "$archive"
rm -f "$archive"

./config.sh \
  --unattended \
  --replace \
  --url "$REPO_URL" \
  --token "$RUNNER_TOKEN" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABEL"

./svc.sh install "$(id -un)"
./svc.sh start
./svc.sh status
