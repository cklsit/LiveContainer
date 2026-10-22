#!/bin/sh
set -eu

# Synology DSM ships libicu but omits the glibc cache utility. The Actions
# runner only checks ldconfig output for libicu during configuration.
echo "libicu"
