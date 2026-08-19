#!/usr/bin/env bash
# Linux CI entry point for the BMC verification platform.
#
# Builds QEMU master + local fault-injection patches, boots the AST2700 A2
# BMC, runs the functional/fault-injection pytest suite, then runs the
# performance regression gate against baseline.json.
#
# Works with GitHub Actions / Jenkins / GitLab CI (any Linux runner).
# Usage:  bash faultinject/ci/run_ci_linux.sh
# Env:
#   SKIP_DEPS=1          skip apt dependency install
#   QEMU_REPO / QEMU_BRANCH   QEMU source (default: gitlab qemu master)
#   IMG_URL              SDK image tarball (default: AspeedTech v11.03)
#   BASELINE             perf baseline file (default: faultinject/baseline.json)
#   SKIP_PERF=1          skip the (slow) perf regression boot
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORKSPACE="${WORKSPACE:-$ROOT}"
QEMU_SRC="$WORKSPACE/qemu-master"
BUILD_DIR="$QEMU_SRC/build-ci"
QEMU_REPO="${QEMU_REPO:-https://gitlab.com/qemu-project/qemu.git}"
QEMU_BRANCH="${QEMU_BRANCH:-master}"
PATCH="$WORKSPACE/faultinject/patches/qemu-master-local.patch"
IMG_URL="${IMG_URL:-https://github.com/AspeedTech-BMC/openbmc/releases/download/v11.03/ast2700-default-image.tar.gz}"
IMG_DIR="$WORKSPACE/images/ast2700-default-image"
BASELINE="${BASELINE:-$WORKSPACE/faultinject/baseline.json}"
PY="python3"

log() { echo -e "\n=== $* ==="; }

# 1) dependencies -----------------------------------------------------------
if [ "${SKIP_DEPS:-0}" != "1" ]; then
  log "installing build/test dependencies"
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    build-essential pkg-config meson ninja-build python3 python3-pip python3-pytest \
    libglib2.0-dev libpixman-1-dev zlib1g-dev libslirp-dev libfdt-dev \
    libpng-dev libjpeg-dev flex bison git curl
fi

# 2) QEMU source + local patches --------------------------------------------
log "fetching QEMU $QEMU_BRANCH"
if [ ! -d "$QEMU_SRC/.git" ]; then
  git clone --depth 1 --branch "$QEMU_BRANCH" "$QEMU_REPO" "$QEMU_SRC"
else
  git -C "$QEMU_SRC" fetch --depth 1 origin "$QEMU_BRANCH"
  git -C "$QEMU_SRC" checkout -f FETCH_HEAD
fi

log "applying local fault-injection patches"
if git -C "$QEMU_SRC" apply --check "$PATCH" 2>/dev/null; then
  git -C "$QEMU_SRC" apply "$PATCH"
else
  echo "note: patch already applied or conflicts; continuing with current tree"
fi

# 3) build ------------------------------------------------------------------
log "building aarch64-softmmu"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
if [ ! -f build.ninja ]; then
  ../configure --target-list=aarch64-softmmu \
    --enable-slirp --enable-plugins --disable-werror
fi
ninja -j"$(nproc)" qemu-system-aarch64

# 4) firmware image ---------------------------------------------------------
log "fetching OpenBMC SDK image"
mkdir -p "$WORKSPACE/images"
if [ ! -f "$IMG_DIR/image-bmc" ]; then
  curl -fL --retry 3 -o /tmp/ast2700-image.tar.gz "$IMG_URL"
  tar -xzf /tmp/ast2700-image.tar.gz -C "$WORKSPACE/images"
fi

# 5) control-plane unit checks ----------------------------------------------
log "control-plane integration checks (fake QMP server)"
$PY "$WORKSPACE/faultinject/tests/verify_control_plane.py"

# 6) functional + fault-injection suite -------------------------------------
log "functional/fault-injection pytest suite (boots AST2700 A2)"
export QEMU="$BUILD_DIR/qemu-system-aarch64"
export IMG="$IMG_DIR/image-bmc"
$PY -m pytest "$WORKSPACE/faultinject/test_bmc_functional.py" -v

# 7) performance regression gate --------------------------------------------
if [ "${SKIP_PERF:-0}" != "1" ]; then
  log "performance regression (icount + x-query-jit), baseline: $BASELINE"
  cd "$WORKSPACE"
  $PY faultinject/perf_regression.py \
      --baseline "$BASELINE" --out result.json \
      || { echo "PERF REGRESSION GATE FAILED (see result.json)"; exit 1; }
  echo "PERF GATE PASSED"
fi

log "BMC platform CI: ALL STAGES PASSED"
