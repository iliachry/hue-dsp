#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_BIN="${1:-$HOME/.local/bin/hue-dsp-stream}"
mkdir -p "$(dirname "$OUT_BIN")"

OPENSSL_PREFIX="${OPENSSL_PREFIX:-/opt/homebrew/opt/openssl@3}"
if [ ! -d "$OPENSSL_PREFIX" ] && [ -d "/usr/local/opt/openssl@3" ]; then
    OPENSSL_PREFIX="/usr/local/opt/openssl@3"
fi

TMP_OBJ="$(mktemp -t dtls_hue).o"
trap 'rm -f "$TMP_OBJ"' EXIT

/usr/bin/clang -O2 -std=c11 -Wall -Wextra -Wpedantic -Werror \
    -I"$OPENSSL_PREFIX/include" \
    -c "$DIR/dtls_hue.c" -o "$TMP_OBJ"

/usr/bin/swiftc -O -swift-version 6 -parse-as-library -strict-concurrency=complete -warnings-as-errors \
    "$DIR/hue_dsp_stream.swift" \
    "$TMP_OBJ" \
    -L"$OPENSSL_PREFIX/lib" -lssl -lcrypto \
    -framework CoreAudio \
    -framework AudioToolbox \
    -framework Accelerate \
    -framework CoreGraphics \
    -framework ImageIO \
    -o "$OUT_BIN"

echo "Built $OUT_BIN"
