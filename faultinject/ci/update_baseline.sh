#!/usr/bin/env bash
# Update the perf regression baseline after an *intentional* image/config
# change (e.g. new firmware, new QEMU feature that shifts boot metrics).
# Usage: bash faultinject/ci/update_baseline.sh [result.json]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${1:-$ROOT/result.json}"
DST="$ROOT/faultinject/baseline.json"

[ -f "$SRC" ] || { echo "no $SRC - run perf_regression.py first"; exit 1; }
cp "$SRC" "$DST"
echo "baseline updated: $SRC -> $DST"
echo
echo "next:  git add faultinject/baseline.json"
echo "       git commit -m 'perf: update baseline after <change>' && git push"
