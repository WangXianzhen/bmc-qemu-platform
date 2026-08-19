# QEMU 本地补丁清单（faultinject/patches）

> 目标：仅做"接口暴露"，不改变 guest 可见默认行为；全部可摘除、可对齐上游。
> QEMU 基线：master fa19879d（2026-08-17）

## P2 — LTPI 链路故障注入属性（✅ 已实现并实机验证）

| 项 | 值 |
|---|---|
| 文件 | `hw/misc/aspeed_ltpi.c`、`include/hw/misc/aspeed_ltpi.h` |
| 属性 | `link-down:bool`、`fault-code:uint32` —— **运行时属性**（`object_property_add_bool` / `object_property_add_uint32_ptr`，realize 后可用 QMP qom-set） |
| 行为 | `link-down=true` 时读 LTPI link-management 寄存器（ctrl 字偏移 0x42）强制清 link-up 位（bit0）；`fault-code!=0` 时读 fault-status 寄存器（ctrl 字偏移 0x41）OR 入该值 |
| 控制面入口 | `/machine/soc/ltpi-ctrl[0..1]`（qmp_client.ltpi_link_down / ltpi_fault_code） |
| 实机验证 | ✅ 2026-08-19 在真实 AST2700 A2 + OpenBMC 实例上 qom-set 通过（live_fault_smoke.py: `link-down=True`、`fault-code=0xdead`） |
| 说明 | 寄存器位布局按 AST2700 LTPI 描述；如与实际 datasheet 位定义不符，调整 `ASPEED_LTPI_LINK_MNG_IDX` / `ASPEED_LTPI_LINK_UP_BIT` / `ASPEED_LTPI_FAULT_IDX`（aspeed_ltpi.h） |
| 注意 | 新版 QEMU 属性 API 变化：`DeviceClass.props` → `device_class_set_props()`（宏需 `const` 数组且无终结符）；运行时属性必须用 `object_property_add_*`（DEFINE_PROP 只允许 realize 前设置） |

## BUILD-FIX — Windows(msys2) 构建兼容（本地环境专用）

| 项 | 值 |
|---|---|
| 文件 | `scripts/symlink-install-tree.py` |
| 问题 | Windows 无 Developer Mode/管理员权限时不允许普通符号链接，且 msys2 的裸 `meson` 脚本无法被 CreateProcess 启动 |
| 修改 | ① introspect 启动失败时回退 `python -m mesonbuild.mesonmain`；② 符号链接失败（非 EEXIST）时在 Windows 上跳过 qemu-bundle 打包树（该树仅安装期 DLL 打包需要） |
| 状态 | 已应用，configure/编译/实机运行全部通过（gcc 16.2.0 / mingw64 / Python 3.14.7 / ninja 1.13.2） |

## P3 — SDMC DRAM ECC 故障注入（✅ 已实现并实机验证）

| 项 | 值 |
|---|---|
| 文件 | `hw/misc/aspeed_sdmc.c`、`include/hw/misc/aspeed_sdmc.h` |
| 属性 | `inject-ecc-error:bool`（触发）、`ecc-error-addr:uint32`（报告地址）、`ecc-fail-status:uint32`（只读，映射 SDMC 寄存器） |
| 行为 | `inject-ecc-error=true` → `R_ECC_FAIL_STATUS`(0x78) 置错误位、`R_ECC_FAIL_ADDR`(0x7c) 写入注入地址（guest 可 devmem 0x12c00078/7c 读取）；`false` 清除 |
| 说明 | 物理 SError 注入在 TCG ARM 未实现（KVM 才支持 serror.pending），故故障经 SDMC 状态寄存器暴露（贴合 SoC 错误路径） |
| 实机验证 | ✅ 2026-08-19：`status=0x1 addr=0x12345678`，清除后 `status=0x0` |

## P4 — PECI host 故障注入（✅ 已实现，设备级验证）

| 项 | 值 |
|---|---|
| 文件 | `hw/misc/aspeed_peci.c`、`include/hw/misc/aspeed_peci.h` |
| 属性 | `host-lost:bool`、`temp-fault:bool`（运行时属性，`/machine/soc/peci`） |
| 行为 | `host-lost=true` → PECI FIRE 命令永不完成（guest 观察超时，模拟 host CPU 掉线）；`temp-fault=true` → 命令以错误完成码 0xE0 完成（模拟 host 故障） |
| 实机验证 | ✅ 2026-08-19 在 ast2600-evb（已例化 PECI）上验证属性注册/设置/清除 |
| AST2700 接线 | ✅ 已补：`hw/arm/aspeed_ast27x0.c` 例化 peci（基址 **0x14C1F000**，取自 AST2700 A2 Datasheet V1.2 与 AspeedTech 内核 `peci-controller@14c1f000`，irq `intc1_5 bit4`=GIC197 与 QEMU irqmap 一致），`/machine/soc/peci` 属性 set/get 实测通过 |

## P5 — max31785 风扇故障注入（✅ 已实现并实机验证）

| 项 | 值 |
|---|---|
| 文件 | `hw/sensor/max31785.c` |
| 属性 | `fan-fault-mask:uint16`（bit i = 风扇页 i 故障，`/machine/peripheral/fan-ctl`） |
| 行为 | 故障位置位后：`READ_FAN_SPEED_1` 返回 0（堵转）；`STATUS_FANS_1_2`（页 0/1）OR 入故障位 |
| 实机验证 | ✅ 2026-08-19：mask 0x1 设置/清除均通过 |

## 已取消的 P1 — AER 注入（主线已有，无需补丁）

- 主线已提供 HMP `pcie_aer_inject_error`（hw/pci/pci-hmp-cmds.c:173；hmp-commands.hx:1286），支持 `-c`（可纠正）、`-a`（advisory non-fatal）、`header0..3`、`prefix0..3`；
- 控制平面经 QMP `human-monitor-command` 调用（qmp_client.aer_inject）。
- **实机验证** ✅ 2026-08-19：`pcie_aer_inject_error nic0 0x4000` 返回 `OK id: nic0 root bus: 0002:00`。

## 机器级缺口（非补丁可解）

- `inject-nmi`：`nmi_inject()`（hw/core/nmi.c:44）遍历 QOM 树找 `TYPE_NMI` 接口；AST2700 无该设备，且 Cortex-A35 模型无 FEAT_NMI（GICv3 `has-nmi` 仅在 TCG+aa64_nmi 时暴露，见 hw/arm/virt.c:1272）。**NMI 注入在 AST2700 上不可用**；替代：watchdog `pause`/`reset` 动作，或 P3 SError 注入。

## 待办（后续轮次）

- 双 QEMU 联动拓扑（x86 host guest + BMC 互联，IPMB/PECI 桥）
- 存储注错完整用例（werror/truncate → VM pause）在 Linux CI 的 pytest 中跑通（`test_bmc_functional.py` 已含用例）
- Redfish 延迟指标打通（需 PCIe2 fdt workaround 使 eth2 获得 IP）

## 补丁文件与 CI

- 全部本地改动已导出为独立补丁：`faultinject/patches/qemu-master-local.patch`（`git apply` 应用；reverse-check 校验通过）
- Linux CI 集成：`faultinject/ci/run_ci_linux.sh` + `.github/workflows/bmc-qemu-ci.yml`（构建 → 控制平面检查 → AST2700 pytest → perf 回归门禁）