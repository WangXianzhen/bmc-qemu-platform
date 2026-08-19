#!/usr/bin/env bash
# Launch the DUT platform: BMC (AST2700 A2) + emulated managed-platform components.
#
# Usage:
#   QEMU=/path/to/qemu-system-aarch64 \
#   IMG=/path/to/ast2700-default/image-bmc \
#   QMP_ADDR=tcp:127.0.0.1:4444   (Windows) | unix:/tmp/ast2700-qmp.sock (Linux)
#   bash faultinject/launch_ast2700.sh [extra qemu args...]
#
# Firmware: AspeedTech OpenBMC SDK v11.03 (ast2700-default image-bmc)
#   https://github.com/AspeedTech-BMC/openbcm/releases/tag/v11.03
# Verified: QEMU master fa19879d (local patches P2) on Windows/msys2 + Linux.
set -euo pipefail

QEMU="${QEMU:-./qemu-master/build-mingw/qemu-system-aarch64.exe}"
IMG="${IMG:-images/ast2700-default-image/image-bmc}"
QMP_ADDR="${QMP_ADDR:-tcp:127.0.0.1:4444}"     # Windows builds: tcp only
CONSOLE_LOG="${CONSOLE_LOG:-ast2700-console.log}"
NVME_IMG="${NVME_IMG:-nvme.img}"
PC_BIOS="${PC_BIOS:-./qemu-master/pc-bios}"    # -L dir (vbootrom/ast27x0_bootrom.bin)

[ -x "$QEMU" ] || { echo "QEMU not found: $QEMU (set QEMU=...)"; exit 1; }
[ -f "$IMG" ]  || { echo "firmware image not found: $IMG (set IMG=...; download SDK v11.03)"; exit 1; }
[ -f "$NVME_IMG" ] || { dd if=/dev/zero of="$NVME_IMG" bs=1M count=4096 2>/dev/null \
                        || echo "note: create $NVME_IMG (4G) yourself"; }

rm -f "$CONSOLE_LOG"

exec "$QEMU" \
  -machine ast2700-evb \
  -smp 4 \
  -m 2G \
  -drive file="$IMG",format=raw,if=mtd \
  \
  # --- emulated managed-platform components (design doc §3) ---
  # I2C bus 0: EVB built-in LM75/TMP105 at 0x4d (do not re-add)
  # I2C bus 1: PSU monitor (PMBus; vin/vout/iout qom props)
  -device adm1272,bus=aspeed.i2c.bus.1,address=0x10,id=psu0 \
  # I2C bus 1: extra temp sensor (fault-injection target)
  -device tmp105,bus=aspeed.i2c.bus.1,address=0x4d,id=temp-mb \
  # I2C bus 2: fan controller (PMBus register model)
  -device max31785,bus=aspeed.i2c.bus.2,address=0x52,id=fan-ctl \
  # I2C bus 4: GPIO expander (LEDs / presence)
  -device pca9552,bus=aspeed.i2c.bus.4,address=0x60,id=gpio-exp0 \
  \
  # PCIe RC2: BMC management NIC (e1000e = Intel 82574L)
  -device e1000e,netdev=net0,bus=pcie.2,id=nic0 \
  -netdev user,id=net0,hostfwd=tcp::2443-:443,hostfwd=tcp::2222-:22 \
  # PCIe RC1: storage (block-layer error injection via rerror/werror)
  -device nvme,serial=SN0001,drive=nvmedrv,bus=pcie.1,id=nvme0 \
  -drive file="$NVME_IMG",if=none,id=nvmedrv,format=raw,\
         rerror=report,werror=stop \
  \
  # --- fault-injection / perf infrastructure ---
  # NMI/SError note: ast2700 (Cortex-A35) lacks FEAT_NMI, so prefer
  # watchdog 'pause'/'reset' actions; SError injection is patch P3.
  -watchdog-action pause \
  -qmp "$QMP_ADDR",server=on,wait=off \
  -L "$PC_BIOS" \
  -display none -serial file:"$CONSOLE_LOG" \
  "$@"
