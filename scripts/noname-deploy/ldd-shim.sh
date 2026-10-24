#!/bin/sh
set -eu

if [ -x /lib64/ld-linux-x86-64.so.2 ]; then
  loader=/lib64/ld-linux-x86-64.so.2
elif [ -x /lib/ld-linux-x86-64.so.2 ]; then
  loader=/lib/ld-linux-x86-64.so.2
else
  echo "Cannot locate the dynamic loader required by the GitHub Actions runner" >&2
  exit 1
fi

exec "$loader" --list "$@"
