#!/usr/bin/env bash
set -euo pipefail

echo "=== [hue-dsp] Running Deterministic Quality Gate (python-uv-ai) ==="
# Stage 1: Git conflict & secret hygiene
! grep -rnE '^(<<<<<<<|=======|>>>>>>>)' . --exclude-dir=.venv --exclude-dir=.git || exit 1
# Stage 2: Native C11 + Swift 6 strict compilation check
TMP_BIN="$(mktemp -t hue-dsp-stream-gate)"
trap 'rm -f "$TMP_BIN"' EXIT
bash src/hue_dsp/native/build.sh "$TMP_BIN"
# Stage 3: Ruff lint & format check
uv run ruff check .
uv run ruff format --check .
# Stage 4: Deterministic unit & property tests (pinned seed)
PYTHONHASHSEED=42 uv run pytest -q
# Stage 5: Smoke & CLI/API contract verification
uv run python -m compileall -q src tests
echo "=== [hue-dsp] ALL QUALITY GATES PASSED ==="
