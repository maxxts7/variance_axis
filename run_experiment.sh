#!/usr/bin/env bash
set -euo pipefail

PRESET="${1:-sanity}"
SINGLE="${2:-}"

if [ "$SINGLE" = "--single" ] || [ "$SINGLE" = "-s" ]; then
    echo "Running experiment with preset: $PRESET (single GPU)"
    python run_generation.py --preset "$PRESET"
else
    echo "Running experiment with preset: $PRESET (2-GPU parallel)"
    python run_parallel.py --preset "$PRESET"
fi
