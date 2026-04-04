#!/usr/bin/env bash
set -euo pipefail

PRESET="${1:-sanity}"

echo "Running experiment with preset: $PRESET"
python run_generation.py --preset "$PRESET"
