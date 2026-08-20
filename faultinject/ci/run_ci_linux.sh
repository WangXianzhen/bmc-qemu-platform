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
# Pin the exact QEMU revision the platform was verified against (local build
# = fa19879d). Daily master drift changes TCG/PCIe behavior; override with
# QEMU_REV=<sha> to test a different commit (or unset to follow master).
QEMU_REV="${QEMU_REV:-fa19879df1658f96ac07365fca8835b7decd6995}"
PATCH="$WORKSPACE/faultinject/patches/qemu-master-local.patch"
IMG_URL="${IMG_URL:-https://github.com/AspeedTech-BMC/openbmc/releases/download/v11.03/ast2700-default-image.tar.gz}"
IMG_DIR="$WORKSPACE/images/ast2700-default-image"
BASELINE="${BASELINE:-$WORKSPACE/faultinject/baseline.json}"
PY="python3"
RUN_DIAG="${RUN_DIAG:-0}"     # run the U-Boot/PCIe diagnostic step
DIAG_LOG="${DIAG_LOG:-$WORKSPACE/diag-uboot.log}"

log() { echo -e "\n=== $* ==="; }

# 1) dependencies -----------------------------------------------------------
if [ "${SKIP_DEPS:-0}" != "1" ]; then
  log "installing build/test dependencies"
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    build-essential pkg-config ninja-build python3 python3-pip python3-pytest \
    libglib2.0-dev libpixman-1-dev zlib1g-dev libslirp-dev libfdt-dev \
    libpng-dev libjpeg-dev flex bison git curl
  # QEMU 11.x requires meson >= 1.2; pip version avoids distro staleness
  $PY -m pip install -q --break-system-packages meson
fi

# 2) QEMU source (pinned revision) + local patches ---------------------------
log "fetching QEMU $QEMU_REV (branch $QEMU_BRANCH)"
if [ ! -d "$QEMU_SRC/.git" ]; then
  git init -q "$QEMU_SRC"
  git -C "$QEMU_SRC" remote add origin "$QEMU_REPO"
fi
if ! git -C "$QEMU_SRC" fetch -q --depth 1 origin "$QEMU_REV" 2>/dev/null; then
  echo "note: could not fetch exact revision, falling back to $QEMU_BRANCH"
  git -C "$QEMU_SRC" fetch -q --depth 1 origin "$QEMU_BRANCH"
fi
git -C "$QEMU_SRC" checkout -q -f FETCH_HEAD
git -C "$QEMU_SRC" log -1 --format="pinned QEMU: %h %ci %s"

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

log "IPMB bridge codec self-test (VM protocol vectors)"
$PY "$WORKSPACE/faultinject/dual/ipmb_bridge.py" --self-test

# 6) functional + fault-injection suite -------------------------------------
# Note: pytest failures must NOT abort the run here: the U-Boot/PCIe diag
# step below is the independent evidence source for the CI PCIe-enumeration
# question, and a flaky fixture boot (login timeout) must not hide it. We
# record the pytest exit code and combine it with diag at the end.
log "functional/fault-injection pytest suite (boots AST2700 A2)"
export QEMU="$BUILD_DIR/qemu-system-aarch64"
export IMG="$IMG_DIR/image-bmc"
PYTEST_RC=0
$PY -m pytest -rs "$WORKSPACE/faultinject/test_bmc_functional.py" -v \
    || PYTEST_RC=$?

# 6b) U-Boot/PCIe diagnostic (opt-in; RUN_DIAG=1) ----------------------------
# Reproduces the PCIe2 fdt workaround step-by-step and reports the guest's
# cmdline / PCI devices / nvme presence, to compare Windows vs Linux builds.
if [ "${RUN_DIAG:-0}" = "1" ]; then
  log "U-Boot/PCIe diagnostic (diag_uboot_pcie.py)"
  export QEMU_DIAG="$BUILD_DIR/qemu-system-aarch64"
  export IMG_DIAG="$IMG_DIR/image-bmc"
  export DIAG_LOG="$DIAG_LOG"
  $PY "$WORKSPACE/faultinject/tests/diag_uboot_pcie.py" \
      || { echo "DIAG FAILED (see $DIAG_LOG)"; DIAG_RC=1; }
fi
DIAG_RC="${DIAG_RC:-0}"

# 7) performance regression gate --------------------------------------------
if [ "${SKIP_PERF:-0}" != "1" ]; then
  log "performance regression (icount + x-query-jit), baseline: $BASELINE"
  cd "$WORKSPACE"
  $PY faultinject/perf_regression.py \
      --baseline "$BASELINE" --out result.json \
      || { echo "PERF REGRESSION GATE FAILED (see result.json)"; PERF_RC=1; }
  if [ "${PERF_RC:-0}" = "0" ]; then
    echo "PERF GATE PASSED"
  fi
fi
PERF_RC="${PERF_RC:-0}"

# 8) combined result ---------------------------------------------------------
# Any of pytest / diag / perf failing fails the job; the summary makes it
# explicit which stage(s) failed so a flaky fixture boot never hides the
# diag evidence (and vice versa).
log "BMC platform CI: combined result"
echo "pytest rc=$PYTEST_RC  diag rc=$DIAG_RC  perf rc=$PERF_RC"
if [ "$PYTEST_RC" != "0" ] || [ "$DIAG_RC" != "0" ] || [ "$PERF_RC" != "0" ]; then
  echo "BMC platform CI: FAILED (pytest=$PYTEST_RC diag=$DIAG_RC perf=$PERF_RC)"
  exit 1
fi
log "BMC platform CI: ALL STAGES PASSED"
