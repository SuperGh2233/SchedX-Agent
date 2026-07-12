#!/usr/bin/env bash
set -eu

missing=""
for tool in git gcc make; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    missing="$missing $tool"
  fi
done

if ! rpm -q openssl-devel >/dev/null 2>&1; then
  missing="$missing openssl-devel"
fi

if [ -n "$missing" ]; then
  echo "Missing dependencies:$missing"
  echo "Install them with: sudo dnf install -y git gcc make openssl-devel"
  exit 1
fi

workdir="${WRK_BUILD_DIR:-/tmp/schedx-wrk-build}"
rm -rf "$workdir"
git clone https://github.com/wg/wrk.git "$workdir"
make -C "$workdir"
sudo install -m 0755 "$workdir/wrk" /usr/local/bin/wrk

wrk --version || wrk -h

