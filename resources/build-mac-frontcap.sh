#!/usr/bin/env bash
# Compile mac-frontcap.swift into a native binary.
# mac-frontcap captures the frontmost window via ScreenCaptureKit (macOS
# 12.3+) — bypasses the bounds-calculation tangle of the mss + Quartz
# crop path. Safe to run on non-macOS — exits silently.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/mac-frontcap.swift"
OUT="${SCRIPT_DIR}/mac-frontcap"

if [[ ! -f "${SRC}" ]]; then
  echo "[mac-frontcap] Source not found: ${SRC}" >&2
  exit 1
fi

if [[ -f "${OUT}" && "${OUT}" -nt "${SRC}" ]]; then
  echo "[mac-frontcap] Binary is up to date, skipping compile."
  exit 0
fi

ARCH=$(uname -m)
if [[ "${ARCH}" == "arm64" ]]; then
  TARGET="arm64-apple-macos12.3"
else
  TARGET="x86_64-apple-macos12.3"
fi

CACHE_DIR="/tmp/clang-module-cache"
mkdir -p "${CACHE_DIR}"

echo "[mac-frontcap] Compiling ${SRC} → ${OUT}"
if ! CLANG_MODULE_CACHE_PATH="${CACHE_DIR}" swiftc \
     "${SRC}" -o "${OUT}" \
     -O -parse-as-library -target "${TARGET}" -swift-version 5 \
     -framework ScreenCaptureKit \
     -framework Cocoa \
     -framework CoreGraphics \
     -framework CoreImage \
     -framework ImageIO \
     -framework UniformTypeIdentifiers; then
  echo "[mac-frontcap] swiftc failed." >&2
  echo "[mac-frontcap] Install Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi

echo "[mac-frontcap] Done."
