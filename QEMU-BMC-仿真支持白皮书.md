# QEMU BMC 及服务器平台仿真支持现状白皮书

> 调研性质：源码级验证（QEMU master 全量克隆）+ 官方文档 + 上游 commit 记录 + 联网交叉验证
> 调研人视角：BMC 固件团队切换 QEMU 版本决策支撑

---

## 1. 版本基线声明

| 项目 | 值 |
|---|---|
| 调研日期 | **2026-08-19** |
| QEMU master 基线 | **fa19879df1658f96ac07365fca8835b7decd6995**（2026-08-17，`Merge tag 'pull-block-jobs-2026-08-17'`） |
| 最新稳定版 | **11.1.0**（2026-08-11 发布，官方公告 https://www.qemu.org/2026/08/11/qemu-11-1-0/ ） |
| 上一稳定版 | 11.0.x（11.0.0 于 2026-04-22 发布；11.0.2 于 ~2026-06 进入 Debian unstable） |
| 源码验证方式 | `git clone --depth 1 --branch master https://gitlab.com/qemu-project/qemu.git` + `git fetch --shallow-since=2023-01-01` 扩展历史，用于核对合入时间线 |
| 验证范围 | hw/arm、hw/riscv、hw/i386、target/i386、target/riscv、hw/scsi、hw/net、docs/system、tests/functional |

> 结论前置：**11.1.0 已包含 AST2700 A2 全部支持（含 `ast2700-evb` 别名指向 A2）；huygens-bmc、Axiado AX3000 尚未进入任何稳定版，仅在 master（下一个 11.2）。**

---

## 2. 逐芯片支持状态矩阵表

| 芯片 | 对应 Machine 名称 | 支持架构 | 上游合入时间 | 已知缺陷 / 限制 | 能否启动 OpenBMC 主线镜像 |
|---|---|---|---|---|---|
| AST2500 | `ast2500-evb`、`romulus-bmc`、`witherspoon-bmc`、`g220a-bmc`、`tiogapass-bmc`、`yosemitev2-bmc`、`supermicrox11spi-bmc` | ARM11 (ARM1176JZS) | 2016（ASPEED 家族最早，`hw/arm: Add ASPEED AST2400 SoC model` 2016-03-17，QEMU 2.6 起） | 见 §3.6 共性缺口（PWM/Fan、Slave GPIO、eSPI 等） | ✅ `evb-ast2500`、`romulus` 等（OpenBMC `supported-machines.md` 收录；QEMU 功能测试 `tests/functional/arm/test_aspeed_romulus.py` 等） |
| AST2600 | `ast2600-evb`、`rainier-bmc`、`fuji-bmc`、`bletchley-bmc`、`fby35-bmc`、`anacapa-bmc`、`gb200nvl-bmc`、`catalina-bmc` | ARM (Cortex-A7 x2) | 2019（`aspeed: Add support for the AST2600 SoC` 系列 2019-09-19 起，QEMU 4.2/5.0 窗口） | eMMC 经 `-drive if=sd` 启动有分区限制（docs/system/arm/aspeed.rst "as of QEMU-10.0"）；PWM/Fan 缺失 | ✅ `evb-ast2600` 等；OpenBMC 内建 QEMU 启动配置（`QB_MACHINE:p10bmc = "-machine rainier-bmc"`，meta-ibm p10bmc.conf） |
| AST2700 A1 | `ast2700a1-evb` | ARM64 (Cortex-A35 x4) | 2024-06-16（commit `5dd883ab0`，QEMU 9.1） | PCIe2 需 U-Boot 内 fdt 补丁 + 手工拷贝镜像（见 §3.5）；SDMC 默认解锁（临时方案）；DPMCU/DP 未模拟 | ❌ 主线 OpenBMC（Linux 主线尚无 aspeed-g7）；✅ AspeedTech fork SDK v11.03 镜像（`ast2700-a1-image`） |
| AST2700 A2 | `ast2700a2-evb`（**别名 `ast2700-evb` 自 QEMU 11.0 起指向 A2**）、`ast2700fc`（Full Chip：A2 + 2x Cortex-M4） | ARM64 (A35 x4 + M4 x2) | A2：2026-02-12（commit `ecfa7ae95` 等，QEMU 11.0）；fc：2025-05-05（commit `a74faf35e`，QEMU 10.1），2026-02 迁移至 A2 | 同 A1；SSP/TSP 仅 `ast2700fc` 才例化 | ❌ 主线 OpenBMC；✅ AspeedTech fork SDK v11.03 镜像（`ast2700-default-image`，QEMU CI 验证到 login） |
| IBM Huygens（AST2700 A2） | `huygens-bmc`（**未合入，v1–v4 评审中**） | ARM64 | —（系列 2026-07-15 首发，5 补丁） | 见 §3.4；附带 CFAM-S/UCD90320/UFS 新模型 | OpenBMC 主线有 `huygens` 机器，但当前基于 p10bmc(AST2600) 栈，非 AST2700 配置 |
| NPCM750/730 | `npcm750-evb`、`quanta-gbs-bmc`、`quanta-gsj`、`kudo-bmc`、`mori-bmc` | ARM (Cortex-A9 x2) | 2020（`hw/arm: Add two NPCM7xx-based machines` 2020-09-14，QEMU 5.2） | **LPC/eSPI 主从接口、KBCI、KCS、BT、虚拟 UART 全部缺失**；PCIe 无；GMAC 已补（2024-02） | ✅ `evb-npcm750`、`gbs`、`kudo`、`mori`（2.18.0）；`gsj` 需 OpenBMC 2.14.0（官方文档明示） |
| NPCM845 | `npcm845-evb` | ARM64 (Cortex-A35 x4) | 2025-02-20（commit `ae0c4d1a1`，QEMU 10.0） | 同 NPCM7xx 缺口 + 8xx 特有项（I3C、温度传感器、虚拟 UART、Flash monitor、JTAG master 缺失）；PCIe RC 补丁 2025-09 仍在评审 | ✅ `evb-npcm845`（OpenBMC 2.18.0） |
| StarFive JH7100 | **无专用 machine**（hw/riscv 无任何 StarFive SoC 模型） | RISC-V (SiFive U74 x2) | — | 无 GPU/VPU/NVDLA 模型，替代方案见 §4 | N/A（JH7100 非 OpenBMC 平台） |
| Axiado AX3000 | `axiado-scm3003` | ARM64 (Cortex-A53 x4) | 2026-08-12（commit `33a71a68c`，**仅 master → QEMU 11.2**） | 极简骨架：仅 GICv3、4x UART、8x GPIO、SDHCI、时钟；**无以太网/I2C/PCIe/USB**（hw/arm/ax3000-soc.c） | ❌ OpenBMC 无此机器 |

> 矩阵依据：master 源码 `hw/arm/` 文件清单、`docs/system/arm/aspeed.rst`、`docs/system/arm/nuvoton.rst`、OpenBMC `meta-phosphor/docs/supported-machines.md`（2026-08 抓取）、GitLab API commit 历史。

---

## 3. AST2700 A1 与 A2 独家深度分析

### 3.1 `ast2700-evb` 别名现状 —— 已默认指向 A2

源码证据（`hw/arm/aspeed_ast27x0_evb.c`）：

```c
/* A1 machine */
mc->desc = "Aspeed AST2700 A1 EVB (Cortex-A35)";   // 行 37
amc->soc_name  = "ast2700-a1";                      // 行 38
...
/* A2 machine */
mc->alias = "ast2700-evb";                          // 行 58 ← 别名挂在 A2 上
mc->desc = "Aspeed AST2700 A2 EVB (Cortex-A35)";    // 行 59
amc->soc_name  = "ast2700-a2";                      // 行 60
mc->default_ram_size = 2 * GiB;                     // 行 70（A1 为 1 GiB，行 48）
```

- **QEMU 11.0（2026-04-22）起，`-M ast2700-evb` 即 A2**。此行为由 commit `bed8917d9`（2026-02-12，"Move ast2700-evb alias to AST2700 A2 EVB"）引入，与 A2 SoC 支持（`ecfa7ae95`）、A2 EVB（`ec270a67d`）同批合入。
- 想用 A1 请显式写 `-M ast2700a1-evb`。
- 历史遗留：`ast2700a0-evb` 机器已于 2026-01-05 删除（`e50a021a4`、`4f53de2f1`、`14ecbe9fb`）。

### 3.2 A1 与 A2 在 QEMU 模拟层的全部差异（逐项）

| 维度 | A1 | A2 |
|---|---|---|
| SoC 类型名 | `ast2700-a1` | `ast2700-a2` |
| SCU silicon-rev 寄存器 | `0x06010103`（include/hw/misc/aspeed_scu.h:55） | `0x06020103`（:56） |
| EVB 默认 RAM | 1 GiB | 2 GiB |
| `ast2700-evb` 别名 | ✗ | ✓ |
| irqmap | `aspeed_soc_ast2700a1_irqmap` | **复用 A1 的 irqmap**（`aspeed_ast27x0.c:1214`，A2 class_init 中仍赋 a1 表） |
| memmap / 外设实例 | 完全相同（4x A35、3x PCIe RC、4x EHCI、3x MAC、13x UART、8x WDT、2x SGPIO、2x IOEXP(AST1700)、LTPI x2、SDHCI、eMMC、HACE、ADC、I2C、GPIO、RTC、Timer、SLI/SLIIO） | 同左 |
| ast2700fc machine | —（fc 已迁移至 A2：`90876d72c`、`b90ce0d78`，BMC RAM 2 GiB） | ✓（`aspeed_ast27x0-fc.c:70` 直接例化 `"ast2700-a2"`） |

结论：**QEMU 模型层 A1→A2 的差异 = silicon-rev 寄存器值 + EVB 默认内存 + 别名**。二者共享同一套 irqmap/memmap 与 SoC realize 路径（`aspeed_soc_ast2700_realize`）。真实硅片 A1/A2 的差异（errata、电气特性等）需查 ASPEED 官方 Datasheet/Errata，QEMU 层不体现——**[待确认]** 建议以 ASPEED 官方 AST2700A2 数据手册核对（AspeedTech 未公开详细 errata 清单）。

### 3.3 A2 是否新增了 M4 协处理器模拟？—— 是的，且不仅限于 A2

- **SSP / TSP 两个 Cortex-M4 协处理器模型于 2025-05-05 合入**（commit `541da2604` "Introduce AST27x0 A1 SSP SoC"、`2d64e6a00` "TSP SoC"、`a74faf35e` "AST2700 A1 full core machine"，QEMU 10.1）。
- 文件：`hw/arm/aspeed_ast27x0-ssp.c`（SSP）、`hw/arm/aspeed_ast27x0-tsp.c`（TSP）、`hw/arm/aspeed_coprocessor_common.c`（抽象基类）、`hw/arm/aspeed_ast27x0-fc.c`（**`ast2700fc` Full Chip machine**）。
- `ast2700fc` 把 4xA35（A2 SoC）+ SSP + TSP 拼成一个 6 CPU 系统（`mc->default_cpus = 6`，fc.c:230）；SSP 用 UART4 作 console、TSP 用 UART7（fc.c:99-104），各带 512 MiB 独立 SDRAM、独立 INTC（NVIC 风格 intcmap）、FMC、OTP、IPC 等。
- 官方功能测试 `tests/functional/aarch64/test_aspeed_ast2700fc.py` 已用 Zephyr 3.7.2 固件（`zephyr-aspeed-ssp.elf`/`zephyr-aspeed-tsp.elf`）验证双 M4 启动，并读取 SSP/TSP SCU 寄存器确认 `[72c02000] 06020103`（A2 rev）。
- 注意：**A1/A2 两个 SoC 模型都含此协处理器框架，但默认 EVB 不例化 M4**；只有 `ast2700fc` 才拉起 SSP/TSP。
- 同时期外设增强（2026-02-04 批）：AST1700（IO die）补齐 SCU/ADC/GPIO/I2C/I3C(unimplemented)/SPI/SRAM/INTC（`b6fb98604` 等 10 笔）、LTPI 控制器接入（`8ed21f4ba`）、**basic Aspeed PWM 模型**（`b935def0f`，仅挂在 AST1700 上）、SGPIO 接入（`508e8630d`）、EHCI IRQ 修正（`7d64f0486`）。

### 3.4 `huygens-bmc` 与标准 EVB 的异同（未合入，评审中）

- 系列：`[PATCH v1 0/5] Add IBM Huygens BMC machine for AST2700`（Mikail Sadic @ IBM，2026-07-15 首发；patchew 显示已有 v2/v3/v4 迭代）。**截至 master fa19879d（2026-08-17）未合入**（`git log --grep=huygens` 无结果）。
- 机器名：`MACHINE_TYPE_NAME("huygens-bmc")`，父类 `TYPE_ASPEED_MACHINE`，**基于 `ast2700-a2` SoC**，2 GiB RAM、UART12 默认 console、vbootrom、自定义 i2c_init。
- 与 `ast2700a2-evb` 的差异（该系列新增 4 个模型）：
  1. **CFAM-S**（`hw/fsi/cfam-s.c`）——AST2700 FSI responder 框架专用实现（IBM FSI 带外管理链路）；
  2. **TI UCD90320 PMBus 电源时序器**（`hw/sensor/ucd90320.c`）；
  3. **AST2700 UFS host controller**（`hw/ufs/aspeed_ufs.c`，约 1095 行）——从 UFS 存储启动 OpenBMC 所必需（QEMU 11.1 已先合入通用 UFS 仿真，Aspeed 专用控制器在本系列中）；
  4. 配套 boot 功能测试 `tests/functional/aarch64/test_aspeed_huygens.py`。
- 验证结果（作者自述）：OpenBMC 经 UFS 启动到 login / Active/Ready。
- OpenBMC 侧状态：`meta-ibm/conf/machine/huygens.conf` 存在，但 `require conf/machine/pstbmc.conf` → p10bmc（**AST2600 栈**），聚焦多 BMC 冗余逻辑（`MACHINE_FEATURES:remove = "op-fsi phal"`）。**即 OpenBMC 主线的 huygens 机器目前仍是 AST2600 配置占位，与 QEMU 补丁面向的 AST2700 A2 真机存在层级错位**——迁移尚未完成 [待确认：IBM 侧未公开说明]。

### 3.5 官方支持/缺失清单与启动方式（docs/system/arm/aspeed.rst，AST2700 节）

- 已支持：INTC、Timer、RTC、I2C、SCU/SCUIO、SRAM、X-DMA、FMC/SPI、USB 2.0 (EHCI)、SD/MMC、SDMC（dummy，源码注释明示"temporarily solution"默认解锁）、WDT、GPIO（仅 Master）、UART、FTGMAC100 以太网、HACE（仅 Hash）、ADC、eMMC（dummy）、PECI（minimal）、I3C、SLI（dummy 寄存器文件，hw/misc/aspeed_sli.c 仅为可读写 regs）。
- **缺失**：PWM/Fan（CPU die 侧）、Slave GPIO、Super I/O、Graphic Display、MCTP、Mailbox、Virtual UART、**eSPI**。LPC 在 AST2700 上已被 SLI/LTPI 取代，KCS/IBT 无对应设备（irqmap 有 192 号映射但无 LPC 设备例化）。
- 官方启动命令（可复制运行，aspeed.rst）：

```bash
# 方式 A：-device loader 手工加载 BL31/OP-TEE/U-Boot（4 核同址启动）
qemu-system-aarch64 -M ast2700-evb \
     -device loader,force-raw=on,addr=0x400000000,file=u-boot.bin \
     -device loader,force-raw=on,addr=0x430000000,file=bl31.bin \
     -device loader,force-raw=on,addr=0x430080000,file=optee/tee-raw.bin \
     -device loader,cpu-num=0,addr=0x430000000 \
     -device loader,cpu-num=1,addr=0x430000000 \
     -device loader,cpu-num=2,addr=0x430000000 \
     -device loader,cpu-num=3,addr=0x430000000 \
     -smp 4 \
     -drive file=image-bmc,format=raw,if=mtd \
     -nographic

# 方式 B：虚拟 bootrom（-bios，默认 ast27x0_bootrom.bin；不带 -bios 也会尝试加载）
qemu-system-aarch64 -M ast2700-evb \
     -drive file=image-bmc,format=raw,if=mtd \
     -nographic

# ast2700fc（A35 + 双 M4；3 个串口：serial0=A35、serial1=SSP、serial2=TSP）
qemu-system-aarch64 -M ast2700fc \
     -device loader,force-raw=on,addr=0x400000000,file=u-boot.bin \
     -device loader,force-raw=on,addr=0x430000000,file=bl31.bin \
     -device loader,force-raw=on,addr=0x430080000,file=optee/tee-raw.bin \
     -device loader,cpu-num=0,addr=0x430000000 \
     -device loader,cpu-num=1,addr=0x430000000 \
     -device loader,cpu-num=2,addr=0x430000000 \
     -device loader,cpu-num=3,addr=0x430000000 \
     -drive file=image-bmc,if=mtd,format=raw \
     -device loader,file=zephyr-aspeed-ssp.elf,cpu-num=4 \
     -device loader,file=zephyr-aspeed-tsp.elf,cpu-num=5 \
     -serial pty -serial pty -serial pty \
     -snapshot -S -nographic
```

- 已知 workaround（官方功能测试 `test_aspeed_ast2700a1.py`/`a2.py` 内）：PCIe2 需在 U-Boot 里 `cp 100420000 403000000 900000` 预置镜像、`fdt set /soc@14000000/pcie@140d0000 status "okay"` 后 `bootm`，说明 **AST2700 PCIe 链路模拟仍不完整**（AST1150 PCI-to-PCI bridge 与挂载的 82574L e1000e 可用，但需手工引导步骤）。
- QEMU CI 验证镜像源：https://github.com/AspeedTech-BMC/openbmc/releases/tag/v11.03（`ast2700-a1-image` / `ast2700-default-image` / `*-dcscm-image`）。

### 3.6 关于 OpenBMC 主线支持 AST2700 的真相

- Linux 上游主线：**aspeed-g7（AST2700）SoC 支持尚未合入**。`[PATCH v9 0/4] Introduce ASPEED AST27xx BMC SoC`（2026-06-09）仍在评审（v0 2025-06 起，历时一年）。
- 因此 OpenBMC 主线（upstream kernel）**无法产出可在 QEMU 上启动的 AST2700 镜像**；AST2700 固件生态目前依赖 **AspeedTech-BMC fork**（openbmc/linux 的 aspeed SDK 分支）。
- OpenBMC 主线的 `yosemite5a7`（Facebook，AST2700）machine 当前仍用 `KERNEL_DEVICETREE = "aspeed/ast2700-evb.dtb"` + `aspeed_g7_defconfig`（ASPEED SDK 内核），2026-04-20 的 commit `daaf018` 才"准备"迁移到 openbmc/linux 内核树。

---

## 4. StarFive JH7100 的实机模拟方案

### 4.1 主线现状：无专用 machine

`hw/riscv/` 现有机器：`sifive_e`、`sifive_u`（HiFive Unleashed / FU540，E51+U54）、`spike`、`virt`、`microchip_pfsoc`、`opentitan`、`k230`、`shakti_c`、`tt_atlantis`、`xiangshan_kmh`、`microblaze-v-generic`。**没有任何 StarFive SoC（JH7100/JH7110）模型**；RISC-V CPU 模型清单中也没有 `sifive-u74`（U74 是 JH7100 的 CPU 核）。网上流传的 VisionFive machine 补丁从未合入。

### 4.2 推荐替代：`virt` machine + 自定义 DTB

`virt` 机器（docs/system/riscv/virt.rst）提供：最多 512 核、CLINT、PLIC、NS16550 UART、Goldfish RTC、virtio-mmio x8、**通用 PCIe host bridge**、fw_cfg、NOR flash，且默认 CPU 带 H 扩展。JH7100 实机为 2x SiFive U74（RV64GC，**无 V 向量扩展**），因此 `-cpu rv64`（QEMU 基础 RV64GC）最贴近；需要 B 扩展/更高 profile 时可用 `-cpu rva22u64` / `rva23u64`（QEMU 具名 profile 模型，cpu.c:2229-2313）。

**自定义 DTB 三要件**（virt.rst 原文要求）：
1. `/cpus` 子节点数必须等于 `-smp`；
2. `/memory` 的 reg 大小必须等于 `-m`；
3. 若用 OpenSBI 作为 -bios，必须含 compatible `"riscv,clint0"` 的 CLINT 节点。

DTB 素材来源：Linux 主线对 JH7100 支持成熟（`arch/riscv/boot/dts/starfive/jh7100.dtsi` + `visionfive-v1.dts`，自 v5.17），可将其裁剪/重映射到 QEMU virt 地址空间（virt 的 UART/CLINT/PLIC/PCIe 地址见 `hw/riscv/virt.c` 与生成的 DTB，可用 `-machine dumpdtb=out.dtb` 导出 QEMU 自带 DTB 作为底稿再叠加 JH7100 节点）。

**可直接运行的启动示例**（OpenSBI + U-Boot + 自定义 DTB + virtio 外设）：

```bash
# 1) 导出 QEMU virt 自带 DTB 作底稿（2 核、2G 内存）
qemu-system-riscv64 -M virt,dumpdtb=base.dtb -cpu rv64 -smp 2 -m 2G

# 2) 基于 base.dtb 叠加 JH7100 板级节点（GPIO/ETH 等；地址与中断须匹配 QEMU 生成值）
#    （手工裁剪 jh7100.dtsi 中 UART/I2C/GPIO 等通用外设节点即可，GPU/VPU 节点删掉，
#      因为 QEMU 无对应模型，保留会导致驱动访问黑洞地址）

# 3) 启动（fw_dynamic.bin=OpenSBI；u-boot.bin 为 S-mode U-Boot 或直接 Linux Image）
qemu-system-riscv64 \
  -M virt \
  -cpu rv64 \
  -smp 2 \
  -m 2G \
  -bios fw_dynamic.bin \
  -kernel u-boot.bin \
  -dtb jh7100-virt.dtb \
  -device virtio-net-pci,netdev=net0 \
  -netdev user,id=net0 \
  -drive file=rootfs.img,format=raw,if=none,id=disk0 \
  -device virtio-blk-pci,drive=disk0 \
  -nographic
```

若只想快速跑 Linux 验证 SoC 无关逻辑（BMC 上层应用），`-kernel Image -initrd rootfs.cpio` + 免 DTB（QEMU 自动生成）即可。

### 4.3 GPU/VPU 等外设模拟缺口评估

JH7100 特色外设（据 JH7100 Datasheet V01.01.04，https://starfivetech.com/uploads/JH7100%20Datasheet.pdf ）：

| 外设 | QEMU 模拟现状 | 结论 |
|---|---|---|
| Imagination **BXE-4-32 GPU** | 无任何 Imagination GPU 模型 | **完全无法模拟**；仅能以 `virtio-gpu`（TCG 下软渲染/virgl 需宿主 GL）替代显示输出，BXE 专有驱动路径不可用 |
| **NVDLA**（深度学习加速器） | 无模型 | 完全无法模拟 |
| **VPU / 视频编解码**（H.264/H.265） | 无模型 | 完全无法模拟 |
| ISP、MIPI CSI/DSI | 无模型 | 完全无法模拟 |
| U74 CPU（RV64GC） | `-cpu rv64` 可近似（无 `sifive-u74` 具名模型） | 功能等价可用 |
| 2x GbE（dwmac）、USB3、PCIe2、CAN | virt 上可用 virtio-net-pci / virtio-blk / PCIe 桥替代 | 接口级可替代，寄存器级不可 |

> 结论：JH7100 适合验证**纯软件逻辑**（U-Boot/Linux/上层应用），所有媒体/加速类外设需实机或 FPGA。若团队目标是 RISC-V BMC 形态验证，建议关注 Linux 侧已成熟的 virt 生态，而非 JH7100 精确仿真。

---

## 5. 服务器 CPU 模拟对比报告（Intel Xeon / AMD EPYC）

### 5.1 master 中可用的 x86 服务器 CPU 模型（target/i386/cpu.c 行号）

| 厂商 | CPU 模型（`-cpu` 值） | 对应实机 | 合入时间 | 备注 |
|---|---|---|---|---|
| Intel | `Skylake-Server`（+`-IBRS`/`-noTSX` 变体） | Xeon Scalable 1st Gen（2016） | 老模型 | cpu.c:4641 |
| Intel | `Cascadelake-Server`（+`-noTSX`） | Xeon Scalable 2nd Gen（2019） | 老模型 | :4786 |
| Intel | `Icelake-Server` | Xeon Scalable 3rd Gen（2020） | 老模型 | :5053 |
| Intel | `SapphireRapids` | Xeon Scalable 4th Gen（2023） | 2022 | :5240 |
| Intel | `GraniteRapids`（+`-v2`） | Xeon 6（Granite Rapids，2024） | 2023-07-07（commit `6d5e9694e`，QEMU 8.1） | :5434 |
| Intel | `SierraForest`（+`-v2`） | Xeon 6 E-core（Sierra Forest，2024） | 2025 | :5836 |
| Intel | `ClearwaterForest` | Xeon 6 E-core（2025） | 2025 | :6017 |
| Intel | `DiamondRapids` | Xeon（Diamond Rapids，2026） | 2025-12-27（commit `7a6dd8bde`，**QEMU 11.0**） | :5632；官方文档 cpu-models-x86.rst.inc:74 |
| Intel | **`EmeraldRapids`** | Xeon Scalable 5th Gen | **从未合入**（2023-06 系列被拒/搁置） | — |
| AMD | `EPYC`（Naples）、`EPYC-Rome`、`EPYC-Milan` | Zen1/Zen2/Zen3 | 老模型 | :6605/:6761/:6861 |
| AMD | `EPYC-Genoa`（+`-v1/-v2`） | Zen4（2022） | 2023 | :6965 |
| AMD | `EPYC-Turin`（+`-v1/-v2`） | Zen5（2024） | 2025-05-28（commit `3771a4daa`，QEMU 10.1）；v2(gmet) 2026-04-30（`1df098483`） | :7217；family 26，xlevel 0x80000022 |

**DiamondRapids 模型要点**（cpu.c:5632 起）：family 0x13 编码、`avx10_version=2`（AVX10.2）、APX（`FEAT_7_1_EDX_APXF` + `FEAT_29_0_EBX_APX`）、AMX 含 FP8/TF32/MOVRS、FRED、LAM、CET；官方文档（cpu-models-x86.rst.inc:74-88）指出其**无 SMT**，缓存层级为 thread/module(DCM)/die(CBB)，可用 `smp-cache` 模拟：

```bash
qemu-system-x86_64 -M pc \
  -cpu DiamondRapids \
  -machine smp-cache.0.cache=l1d,smp-cache.0.topology=thread,\
           smp-cache.1.cache=l1i,smp-cache.1.topology=thread,\
           smp-cache.2.cache=l2,smp-cache.2.topology=module,\
           smp-cache.3.cache=l3,smp-cache.3.topology=die
```

**EPYC-Turin 模型要点**：perfmon-v2（`FEAT_8000_0022_EAX_PERFMON_V2`）、Auto-IBRS、SBPB、SRSO_USER_KERNEL_NO、CLZERO/XSAVEERPTR、SVM 全特性（NPT/VNMI/VGIF…）；`-v2` 追加 `gmet`。

**CCD/CCX 拓扑**：QEMU x86 侧**不模拟 AMD CCD/CCX/NPS 拓扑**（仅 `-smp sockets/cores/threads` + `smp-cache` 近似缓存层级）；EPYC 拓扑精确度依赖 libvirt/宿主侧，BMC 开发场景通常无碍。

### 5.2 模拟双路服务器的完整命令行（q35 + NUMA + PCIe 拓扑）

```bash
# ============ 双路 AMD EPYC-Turin（32C/2S，NUMA 绑定 + 独立内存后端） ============
qemu-system-x86_64 \
  -machine q35 \
  -cpu EPYC-Turin-v1 \
  -smp 32,sockets=2,cores=16,threads=1 \
  -m 64G \
  -object memory-backend-ram,size=32G,id=mem0 \
  -object memory-backend-ram,size=32G,id=mem1 \
  -numa node,nodeid=0,cpus=0-15,memdev=mem0 \
  -numa node,nodeid=1,cpus=16-31,memdev=mem1 \
  -numa dist,src=0,dst=1,val=32 \
  -device pcie-root-port,id=rp0,chassis=0,slot=0,bus=pcie.0 \
  -device pcie-root-port,id=rp1,chassis=1,slot=1,bus=pcie.0 \
  -device virtio-net-pci,netdev=net0,bus=rp0 \
  -netdev user,id=net0 \
  -device nvme,serial=SN0001,drive=osd,bus=rp1 \
  -drive file=disk.qcow2,if=none,id=osd \
  -device mptsas,id=scsi0,bus=rp0 \
  -device scsi-hd,drive=sasd0,bus=scsi0.0,channel=0,scsi-id=0 \
  -drive file=sas.img,format=raw,if=none,id=sasd0 \
  -m 64G \
  -nographic

# ============ 双路 Intel（以 DiamondRapids 为例，无 SMT 的 DCM/CBB 拓扑） ============
qemu-system-x86_64 \
  -machine q35,smp-cache.0.cache=l1d,smp-cache.0.topology=thread,\
           smp-cache.1.cache=l1i,smp-cache.1.topology=thread,\
           smp-cache.2.cache=l2,smp-cache.2.topology=module,\
           smp-cache.3.cache=l3,smp-cache.3.topology=die \
  -cpu DiamondRapids \
  -smp 32,sockets=2,modules=2,cores=8,threads=1 \
  -m 64G \
  -object memory-backend-ram,size=32G,id=mem0 \
  -object memory-backend-ram,size=32G,id=mem1 \
  -numa node,nodeid=0,cpus=0-15,memdev=mem0 \
  -numa node,nodeid=1,cpus=16-31,memdev=mem1 \
  -nographic
```

> 说明：`-numa node,memdev=` 是现行推荐写法（`mem=` 已弃用）；`-smp` 的 `modules` 层级（`-smp ...,modules=N`）与 `smp-cache` 配合可表达 Intel DCM/CBB。q35 机器族版本名：`pc-q35-11.2`（master）/ `pc-q35-11.1`（11.1）/ `pc-q35-11.0`（11.0），`-M q35` 恒指向最新。

### 5.3 CXL 总线（官方示例，docs/system/devices/cxl.rst）

```bash
qemu-system-x86_64 -M q35,cxl=on -m 4G,maxmem=8G,slots=8 -smp 4 \
  -object memory-backend-file,id=cxl-mem1,share=on,mem-path=cxl-pmem.bin,size=256M \
  -object memory-backend-file,id=cxl-lsa1,share=on,mem-path=cxl-lsa.bin,size=256M \
  -device pxb-cxl,bus_nr=12,bus=pcie.0,id=cxl.1 \
  -device cxl-rp,port=0,bus=cxl.1,id=root_port13,chassis=0,slot=2 \
  -device cxl-type3,bus=root_port13,persistent-memdev=cxl-mem1,lsa=cxl-lsa1,id=cxl-pmem0,sn=0x1
```

QEMU CXL 仿真覆盖主机桥/根端口/交换机/Type3 设备、DOE、IDE、AER、MLD 等（cxl.rst），但**不模拟 fabric management（单主机静态配置）**。

### 5.4 服务器常用外设在 BMC 开发场景的完备性

| 外设 | QEMU 设备名 | 模拟完备度 | 备注 |
|---|---|---|---|
| Intel 网卡 | `e1000e`（82574L）、`igb`（**82576**，支持 SR-IOV，docs/system/devices/igb.rst）、`e1000` | 高（igb 官方提示"缺很多功能，仅测过 Linux/DPDK/Windows"） | **无 I350 专用模型**；82574L 是 AST2700 官方测试用的 NIC |
| virtio 网卡 | `virtio-net-pci` | 高 | 默认推荐 |
| NVMe | `nvme`（hw/nvme，含多命名空间/持久内存） | 高 | 服务器场景首选 |
| SAS | `mptsas`（MPT-SAS/SAS1068E 门铃模型，IBM 贡献）、`megasas`（MegaRAID）、`lsi53c895a`、`esp`、`vmw_pvscsi`、`virtio-scsi` | 中（mptsas/megasas 均为简化命令集模型，长 SGL/高级特性受限） | BMC 固件测 SAS 枚举/热插可用 |
| LPC（带外） | q35 用 `lpc_ich9`（ICH9 LPC+SMBus+GPIO+RTC）；AST2500/2600 用 `aspeed_lpc`（KCS/IBT 子集） | 中 | **AST2700 无 LPC**（改 SLI/LTPI）；NPCM7xx/8xx LPC/KCS/BT 缺失 |
| **eSPI** | 无功能模型（仅 npcm7xx.c:789 / npcm8xx.c:778 / aspeed_ast1040.c:128 的 unimplemented 占位） | **无** | 全 QEMU 无 eSPI 仿真 |
| SMBus/I2C | `pm_smbus`（PIIX4）、`smbus_ich9`、`aspeed_i2c`、`npcm7xx_smbus`、`smbus_ipmi` | 高（BMC 场景成熟） | IPMI BT/KCS 之外的 I2C 带外路径可用 |
| GPIO | `aspeed.gpio`（仅 Master）、NPCM GPIO、Cadence GPIO | 中 | Aspeed Slave GPIO 缺失 |
| PWM/风扇 | NPCM7xx/8xx：`TYPE_NPCM7XX_PWM` + `MFT`（测速）可用；Aspeed：basic PWM 仅挂在 AST1700（2026-02 新加），**AST2700 CPU die 侧仍无 PWM/FAN** | NPCM 好 / Aspeed 差 | — |
| 虚拟 UART / MCTP / VDM | 无 | **无** | docs 明示缺失 |

---

## 6. 编译构建指南

### 6.1 覆盖本白皮书全部机器的最小构建（Linux 示例）

```bash
# 依赖（Debian/Ubuntu 为例；11.x 需要 meson>=1.2、ninja、python3.8+）
sudo apt install -y build-essential pkg-config meson ninja-build python3 \
    libglib2.0-dev libpixman-1-dev zlib1g-dev \
    libslirp-dev libpng-dev libjpeg-dev libfdt-dev \
    flex bison

git clone --depth 1 --branch master https://gitlab.com/qemu-project/qemu.git
cd qemu
mkdir build && cd build

../configure \
  --target-list=aarch64-softmmu,arm-softmmu,riscv64-softmmu,x86_64-softmmu \
  --enable-modules \
  --enable-slirp \
  --enable-kvm \
  --enable-debug \
  --disable-werror

ninja -j$(nproc)
```

要点解读：

- **`--target-list`**：`aarch64-softmmu`（AST2700/NPCM845/Axiado/sbsa-ref）、`arm-softmmu`（AST2500/2600、NPCM750）、`riscv64-softmmu`（JH7100 替代方案）、`x86_64-softmmu`（服务器 CPU 对比）。按需可再加 `riscv32-softmmu`。
- **`--enable-modules`**：meson_options.txt 中 `modules` 默认 disabled；开启后可动态加载设备模块，**强烈建议开启**（meson option `modules`，非 Windows）。
- **`--enable-slirp`**：`-netdev user` 需要 libslirp（BMC 开发几乎必用，官方功能测试也依赖 user 网络）。
- **`--enable-kvm`**：仅 Linux 宿主；纯 TCG 模拟（BMC 场景）可不开。
- **`--enable-debug`**：带断言与调试符号，便于定位模拟器自身问题；生产可去掉。
- **`--disable-werror`**：避免新编译器警告阻断构建。
- Windows（MSYS2/Mingw64）构建注意：无 KVM；用 `--disable-kvm`；`--enable-modules` 不适用（meson 注明非 Windows）；建议 `--disable-werror --disable-docs`。
- 验证安装：`./build/qemu-system-aarch64 -M help | grep -E 'ast2700|ast2600|npcm|axiado|huygens'`。

---

## 7. 风险与缺口总结

### 7.1 "已支持但不可用/需 workaround"（broken-ish）

| 项目 | 证据/现象 | 影响 |
|---|---|---|
| AST2700 PCIe2 | 官方功能测试必须 `cp 100420000 403000000 900000` + `fdt set ... pcie@140d0000 status okay` 才能用（test_aspeed_ast2700a2.py） | 默认配置下 PCIe2 不工作；依赖该 RC 的外设需手工引导 |
| AST2700 SDMC | 源码注释：默认 unlocked 是"temporarily solution"（aspeed_ast27x0.c:888-894） | SPL 阶段行为与真机有偏差，从 u-boot 之后启动才稳 |
| eMMC 启动 | aspeed.rst 明示通过 `-drive if=sd` 接入时带 boot 分区会不可访问（"as of QEMU-10.0"） | AST2600/2700 eMMC 镜像制作需按文档特殊处理 |
| AST2700 官方文档陈旧点 | aspeed.rst 仍把 "LPC Peripheral Controller (subset)"、AST2700A1 binaries 写入 ast2700fc 小节；实际 SLI 取代 LPC、fc 已是 A2 | 以源码为准 |
| igb 网卡 | 官方文档自述功能有限，仅验证 Linux/DPDK/Windows 有限用例 | 非 BMC 关键路径 |
| NPCM 文档 vs 代码 | nuvoton.rst "Missing: GMAC"，但 hw/net/npcm_gmac.c（2024-02）已在 npcm8xx.c 例化 | 文档滞后，GMAC 基本可用 |

### 7.2 完全无法用 QEMU 模拟（需实机/FPGA）

- **eSPI 接口仿真**（全线缺失，含 KBCI/KCS/BT/虚拟 UART/eSPI slave）；
- **真实 GPU/VPU/ISP/NVDLA**（Imagination BXE 等所有 SoC 媒体/加速引擎；仅 virtio-gpu 可代显示）；
- **MCTP/VDM/PLDM 传输层**（CXL 信箱内 MCTP-over-VDM 除外）；
- AST2700 **DP/显示处理器（DPMCU = unimplemented）**；
- Aspeed **PWM/Fan（CPU die 侧）与 Slave GPIO**、Super I/O、Mailbox；
- NPCM8xx **PCIe RC**（2025-09 补丁评审中）、安全特性（secure boot/加密引擎细节）；
- **StarFive JH7100/JH7110 全平台**（无 machine、无 U74 具名 CPU）；
- **Intel EmeraldRapids CPU 模型**（从未合入，需用 GraniteRapids/SierraForest/DiamondRapids 替代）；
- AMD **CCD/CCX 精确拓扑**、Intel **SMT 形态的 DiamondRapids**（官方模型即无 SMT）。

### 7.3 基于当前 master 的风险评估结论（给 BMC 团队）

1. **AST2700 A2 已是主线一等公民（QEMU ≥ 11.0）**：`ast2700-evb` 默认 A2，QEMU CI 自带 A1/A2/fc 三套 boot-to-login 功能测试，AspeedTech SDK v11.03 镜像可直接跑；**建议立即基于 11.1.0 或 master 验证**，9.x/10.x 旧版本没有 A2。
2. **若业务需要 IBM Huygens（UFS 启动、CFAM-S、UCD90320）**：必须用 master（11.2 之前）自行应用评审中补丁，或等待合入；OpenBMC 侧 huygens 机器配置仍属 AST2600 占位，需并行推进。
3. **Axiado AX3000 仅 master 可用**且是极简骨架（无网络/I2C），只适合 bring-up 骨架验证，勿作为外设测试平台。
4. **Nuvoton 平台成熟度高于 Aspeed 的带外侧**：NPCM7xx/8xx 有 PWM/MFT/完整 SMBus，但 **LPC/eSPI/KCS/BT 全缺**——若团队依赖 KCS IPMI 通路，两条路线都不满足（AST2700 无 LPC、NPCM 无 KCS），需在实机/FPGA 上覆盖该路径。
5. **服务器侧（x86）**：DiamondRapids/EPYC-Turin 模型齐全，q35+NUMA+CXL 可组双路测试床；无 EmeraldRapids、无 I350 模型（用 82576 `igb` 或 82574L `e1000e` 替代）。
6. **Linux 上游 AST2700 内核支持未合入**是 OpenBMC 主线生态的最大外部依赖（v9 系列 2026-06 仍评审中）；短期只能以 AspeedTech fork 内核为准，风险自担。
7. 总体：**QEMU 11.1（或 master）可作为 AST2700 A2 / AST2600 / NPCM 固件 CI 与 bring-up 的主仿真平台**；OOB（KCS/eSPI/MCTP）、媒体引擎与 JH7100 类平台的缺口必须单独规划实机覆盖。

---

## 附录 A：关键源码/文档路径索引（master fa19879d）

| 主题 | 路径 |
|---|---|
| AST2700 SoC（A1/A2） | hw/arm/aspeed_ast27x0.c（A1/A2 class :1158-1236；A2 复用 a1 irqmap :1214；DPMCU unimplemented :562-563；SDMC 临时解锁 :888-894） |
| AST2700 EVB（别名） | hw/arm/aspeed_ast27x0_evb.c:58 |
| AST2700 FC/SSP/TSP | hw/arm/aspeed_ast27x0-fc.c（A2 :70；6 CPU :230）、aspeed_ast27x0-ssp.c、aspeed_ast27x0-tsp.c、aspeed_coprocessor_common.c |
| silicon-rev 值 | include/hw/misc/aspeed_scu.h:55-56（A1=0x06010103 / A2=0x06020103） |
| SLI（dummy 寄存器） | hw/misc/aspeed_sli.c |
| AST1700（IO die） | hw/arm/aspeed_ast1700.c（含 basic PWM :223） |
| NPCM7xx/8xx | hw/arm/npcm7xx.c、npcm7xx_boards.c、npcm8xx.c、npcm8xx_boards.c；eSPI 占位 npcm8xx.c:778 |
| Axiado AX3000 | hw/arm/ax3000-soc.c、ax3000-evk.c、ax3000-boards.c；include/hw/arm/ax3000-soc.h |
| RISC-V 机器清单 | hw/riscv/*.c（无 JH7100）；virt 说明 docs/system/riscv/virt.rst |
| x86 CPU 模型 | target/i386/cpu.c（DiamondRapids :5632、EPYC-Turin :7217 等）；docs/system/cpu-models-x86.rst.inc |
| 官方 Aspeed/Nuvoton 文档 | docs/system/arm/aspeed.rst、docs/system/arm/nuvoton.rst |
| 功能测试（可启动性证据） | tests/functional/aarch64/test_aspeed_ast2700a1.py / _a2.py / _fc.py、tests/functional/arm/test_aspeed_*.py |

## 附录 B：关键上游链接

- QEMU 11.0.0 发布公告：https://qemu-project.gitlab.io/qemu-web/2026/04/22/qemu-11-0-0/
- QEMU 11.1.0 发布公告：https://www.qemu.org/2026/08/11/qemu-11-1-0/
- AST2700 A2 支持系列（合入 2026-02-12）：https://lists.gnu.org/archive/html/qemu-arm/2026-02/msg00611.html
- Huygens 系列 v1（2026-07-15）：https://patchew.org/QEMU/20260715155037.2237011-1-mikail.sadic@ibm.com/
- Axiado AX3000 系列：https://lists.gnu.org/archive/html/qemu-devel/2026-07/msg03923.html
- Linux aspeed-g7（v9，2026-06-09）：https://patchew.org/linux/20260609-upstream._5Fast2700-v9-0-f631752f0cb1@aspeedtech.com/
- OpenBMC 支持机器清单：https://github.com/openbmc/openbmc/blob/master/meta-phosphor/docs/supported-machines.md
- OpenBMC huygens.conf：https://github.com/openbmc/openbmc/blob/master/meta-ibm/conf/machine/huygens.conf
- OpenBMC yosemite5a7（AST2700 dtb）：https://github.com/openbmc/openbmc/commit/daaf018cc38c1d7f40da4a2ede3b34ce4479913b
- JH7100 数据手册：https://starfivetech.com/uploads/JH7100%20Datasheet.pdf
- AspeedTech OpenBMC SDK 镜像（QEMU 测试用）：https://github.com/AspeedTech-BMC/openbmc/releases/tag/v11.03
