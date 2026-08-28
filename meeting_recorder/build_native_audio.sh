#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export CLANG_MODULE_CACHE_PATH="${TMPDIR:-/tmp}/meeting_recorder_clang_module_cache"

xcrun swiftc \
  -parse-as-library \
  -O \
  -framework ScreenCaptureKit \
  -framework AVFoundation \
  -framework CoreMedia \
  "$ROOT_DIR/native/SystemAudioCapture.swift" \
  -o "$ROOT_DIR/native/system_audio_capture"
