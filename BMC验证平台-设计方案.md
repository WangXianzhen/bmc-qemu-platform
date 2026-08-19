# 基于 QEMU 的 BMC 功能 / 性能 / 异常注错验证平台设计方案

> 版本：v1.0 ｜ 日期：2026-08-19 ｜ QEMU 基线：master fa19879d（含 11.1.0）
> 依据本会话前序《QEMU BMC 及服务器平台仿真支持现状白皮书》与用户确认的范围：
> **被测体 = AST2700 A2（`ast2700-evb` / `ast2700fc`）上运行的 OpenBMC 固件；外部平台（host CPU / 部件）= QEMU 仿真；注错覆盖传感器/电源/风扇、内存/CPU、PCIe/网络、存储、带外通道五类；性能 = 回归式相对指标。**

---

## 1. 设计目标

| 维度 | 目标 |
|---|---|
| 功能验证 | 自动化验证 OpenBMC 在 AST2700 A2 上的启动、传感器读数、风扇策略、IPMI/Redfish、PCIe/NVMe/网络枚举、看门狗等 |
| 异常注错 | 通过控制平面（QMP/qom-set/HMP 封装/qtest）对**仿真出来的"被管理服务器部件"**注入五类故障，验证 BMC 固件的检测、告警、恢复路径 |
| 性能验证 | 确定性运行（`-icount`）下采集 boot-to-login 耗时、guest 指令数（TCG 插件）、Redfish 响应延迟、I2C 事务延迟，做**版本间回归对比**（非绝对基准） |
| 可移植性 | 同一套用例可跑在 11.1.0 稳定版与 master 上；QEMU 本地补丁集中管理、可随时摘除 |

## 2. 总体架构

```
┌────────────────────────── 控制平面（faultinject/） ──────────────────────────┐
│  Python 3 (仅标准库)                                                         │
│  ├─ qmp_client.py      —— 极简 QMP 客户端（qom-set/hmp/device_del/…）        │
│  ├─ fault_matrix.yaml  —— 注错目录（五类 × 机制 × 现成/需补丁）              │
│  ├─ test_*.py          —— pytest 功能 + 注错用例（含 console 交互助手）      │
│  └─ perf_regression.py —— 性能回归采集（-icount + TCG 插件 + trace）         │
└──────────────┬──────────────────────────────┬────────────────────────────────┘
               │ QMP unix socket              │ 启动参数 / 后端文件（可运行时破坏）
┌──────────────▼──────────────────────────────▼────────────────────────────────┐
│ QEMU：qemu-system-aarch64 -M ast2700-evb（被测 BMC）                          │
│   ├── 4× Cortex-A35 + GICv3（含 NMI 布线，见 aspeed_ast27x0.c）              │
│   ├── OpenBMC 固件（AspeedTech SDK v11.03 镜像 / ast2700-default）           │
│   └── 仿真"被管理平台部件"（全部可由控制平面注错）：                           │
│        I2C: tmp105 / adm1272(PSU) / max31785(风扇) / eeprom / pca9552        │
│        PCIe: e1000e / igb / nvme（AST2700 有 3 个 RC：pcie.0/1/2）           │
│        存储: -drive rerror/werror 块层错误                                    │
│        网络: set_link 链路故障 + hostfwd(Redfish/SSH)                        │
│        带外: SLI/LTPI 寄存器模型 + PECI(minimal) + 自定义扩展                │
└──────────────────────────────────────────────────────────────────────────────┘
```

设计要点：
1. **被测体是 BMC 固件本身**——QEMU 的 Aspeed machine 天然就是"BMC SoC 仿真"，部件都挂在 BMC 的 I2C/PCIe/OOB 接口上，与真机拓扑一致（docs/system/arm/aspeed.rst）。
2. **控制平面与仿真解耦**：所有注错走 QMP；QEMU 本地补丁只负责"把内部注错接口暴露成 QMP"，不改任何 guest 可见行为。
3. **同一 launch 脚本 + 不同用例**：启动一次可跑整个套件（fixture 级复用），也可逐用例重启（`-snapshot` 保证干净状态）。

## 3. AST2700 仿真部件拓扑（launch_ast2700.sh 的硬件语义）

| 接口 | 仿真部件 | QEMU 设备 | 注错手段 |
|---|---|---|---|
| I2C bus 0 | 主板温度传感器（EVB 自带 LM75） | `tmp105` @0x4d | `qom-set ... temperature`（官方功能测试已验证） |
| I2C bus 1 | PSU 电源监控（PMBus） | `adm1272`（暴露 `vin/vout/iout` 属性，hw/sensor/adm1272.c:500） | `qom-set vout/iout` → 掉电/过压/过流 |
| I2C bus 2 | 风扇控制器（PMBus） | `max31785`（寄存器级 PMBus 模型） | 寄存器级 + 补丁 P5 加 tach fault 属性 |
| I2C bus 3 | 主板/FRU EEPROM | `smbus-eeprom` @0x50 | 写保护/内容破坏 |
| I2C bus 4 | GPIO 扩展（LED/DIMM 存在） | `pca9552` / `pca9554` | `qom-set` 引脚电平 |
| PCIe RC0 (pcie.0) | 网络（带外管理网） | `e1000e`(82574L) / `igb`(82576) | `set_link` 链路断开（QMP，qapi/net.json:37） |
| PCIe RC1 (pcie.1) | 存储 | `nvme` | 块层 `rerror/werror`；运行时删/截断后端文件；`device_del` 热拔 |
| PCIe RC2 (pcie.2) | 通用（BMC 管理网卡等） | `e1000e` | 同上（官方测试即挂 pcie.2） |
| LPC→SLI/LTPI | 带外通道（取代 LPC） | `aspeed.sli` / `aspeed.ltpi-ctrl`（寄存器级） | 补丁 P2：`link-down`/`fault` 属性 |
| PECI | host CPU 温度/状态接口 | `aspeed-peci`（minimal） | 补丁 P4：PECI host 仿真 + 注错 |
| eSPI | （AST2700 无 eSPI，使用 SLI/LTPI） | — | 无（见 §5.6） |
| GIC/CPU | NMI/看门狗 | GICv3 NMI 已布线（aspeed_ast27x0.c `ARM_CPU_NMI`） | QMP `inject-nmi`、`watchdog-set-action`（qapi/run-state.json:393） |
| DRAM | 内存 | SDMC（dummy）+ DRAM 容器 | 补丁 P3：ECC/uncorrectable 错误注入 |
| x86 host guest（可选联动） | 被管理 host CPU | 第二台 qemu-system-x86_64 | HMP `mce cpu bank status mcgstatus addr misc`（hmp-commands.hx:1511）→ BMC 经 IPMB/PECI 感知 |

## 4. 故障注入能力矩阵（已对照 master 源码核实）

| # | 故障类别 | 具体故障 | 现成机制（源码依据） | 需本地补丁 |
|---|---|---|---|---|
| 1a | 传感器 | 温度越限/恢复 | ✅ `qom-set temperature`（tmp105；官方测试 test_aspeed_ast2700a2.py 实证） | — |
| 1b | 电源 | 掉电/过压/过流 | ✅ `qom-set vout/iout`（adm1272） | — |
| 1c | 风扇 | 转速异常/卡死 | ⚠️ max31785 仅寄存器级 | P5：tach/pwm fault 属性 |
| 1d | 传感器总线 | I2C 从设备无响应 | ⚠️ 需自定义 unimp 设备或 qtest | P5 附带 |
| 2a | 内存 | x86 host guest 的 MCE | ✅ HMP `mce`（hmp-commands.hx:1511，QMP 经 `human-monitor-command`） | —（可选 P6 包装成 QMP） |
| 2b | CPU | NMI 注入 | ✅ QMP `inject-nmi`（qapi/machine.json:433）；AST2700 GICv3 已布 NMI 线 | — |
| 2c | CPU | 看门狗超时动作 | ✅ QMP `watchdog-set-action`（run-state.json:393）+ `-watchdog-action` | — |
| 2d | 内存 | AST2700 guest 内存错误 | ⚠️ 无 | P3：SDMC/DRAM ECC 注入 |
| 3a | PCIe | AER 不可纠正/可纠正 | ⚠️ 内部 API `pcie_aer_inject_error()` 仅 qtest 可达（hw/pci/pcie_aer.c:639） | P1：QMP `pci-inject-error` |
| 3b | PCIe | 设备热拔/消失 | ✅ QMP `device_del` | — |
| 3c | 网络 | 链路断开/恢复 | ✅ QMP `set_link`（qapi/net.json:37） | — |
| 3d | 网络 | 丢包/延迟 | ✅ `-netdev socket` + 外部 tc/netem；或 user 后端 + 控制平面流量整形 | — |
| 4a | 存储 | IO 读写错误 | ✅ `-drive ...,rerror=report,werror=stop`（qemu-options.hx）+ 运行时破坏后端文件触发 | — |
| 4b | 存储 | 热拔 | ✅ `device_del`；SCSI 用 `scsi-hd` 可逐盘拔 | — |
| 4c | 存储 | 性能劣化（限速） | ✅ `blockdev-set-io-throttle`（QMP） | — |
| 5a | 带外 | SLI/LTPI 链路故障 | ⚠️ 仅寄存器读写 | P2：link-down/fault 属性 + 中断 |
| 5b | 带外 | PECI 温度错误/无响应 | ⚠️ minimal 模型 | P4：PECI host 仿真 |
| 5c | 带外 | KCS/BT/eSPI | ❌ QEMU 无功能模型（白皮书 §7.2） | 无法（需实机覆盖或全新开发） |

## 5. QEMU 本地补丁清单（最小集，集中在 faultinject/patches/）

| 补丁 | 修改点 | 接口设计 |
|---|---|---|
| **P1 pci-inject-error** | ~~新增 QMP~~ **不需要**：主线已有 HMP `pcie_aer_inject_error`（hw/pci/pci-hmp-cmds.c:173，hmp-commands.hx:1286，支持 `-c` 可纠正/`-a` advisory/header/prefix），控制平面经 QMP `human-monitor-command` 直接调用（qmp_client.aer_inject） | `{"execute":"human-monitor-command","arguments":{"command-line":"pcie_aer_inject_error nvme0 0x4000"}}` |
| **P2 sli/ltpi-fault** | `hw/misc/aspeed_ltpi.c`、`include/hw/misc/aspeed_ltpi.h` —— **已实现（本地补丁）** | qom 属性 `link-down:bool`（读 LTPI link-mng 寄存器时强制清 link-up 位）、`fault-code:uint32`（读 fault-status 寄存器时 OR 入）；`/machine/soc/ltpi-ctrl[0]` |
| **P3 aspeed-dram-ecc** | `hw/arm/aspeed_ast27x0.c`（dram_container 包装） | qom 属性 `inject-ue:bool`：触发 GIC SError（`qemu_irq` SError 线已有）/ panic 路径，验证 BMC 固件错误处理 |
| **P4 peci-host** | `hw/misc/aspeed_peci.c` | 最小 PECI host（响应 Ping/GetDIB/GetTemp/RdPkgConfig），属性 `temp-fault:bool`、`host-lost:bool` |
| **P5 pmbus-fault** | `hw/sensor/max31785.c`（或新增 `pmbus-psu`） | tach/pwm/status 寄存器故障位 + 对应 qom 属性 |
| **P6（可选）qmp-mce** | `qapi/run-state.json` + `target/i386/` | 把 HMP `mce` 包装为 QMP（QMP-only 控制平面便利性） |

> 补丁原则：**只做"接口暴露"，不改变 guest 可见的默认行为**；全部补丁默认关闭（属性未设置时行为与主线一致），便于随时对齐上游。

## 6. 功能验证方案（pytest）

- 框架：`pytest` + 标准库（`subprocess` 启动 QEMU、`socket` 走 QMP、stdin/stdout 走 serial console）。
- fixture：`launch_ast2700()` —— 启动 `launch_ast2700.sh` 同款参数，返回 `(QMPClient, Console)`；`teardown` 中 `quit` + kill。
- console 助手：`wait_for(pattern)` / `exec_cmd(cmd, expect)` —— 复用 QEMU 官方功能测试模式（tests/functional/ 的 `exec_command_and_wait_for_pattern`）。
- 用例清单（首批）：
  1. `test_boot_to_login`（U-Boot 2023.10 → login，官方镜像）
  2. `test_sensor_fault_injection`（qom-set temp=18000 → guest hwmon 读数变化；再设越限 → phosphor-hwmon 日志告警）
  3. `test_psu_fault_injection`（qom-set adm1272 vout → 掉电告警）
  4. `test_nic_link_down`（set_link off → guest 网卡 down；on → 恢复）
  5. `test_nvme_enum_and_error`（lspci/nvme list；truncate 后端 → werror=stop → QMP 状态 paused → 恢复）
  6. `test_watchdog_action`（guest 停止喂狗 → 按配置动作）
  7. `test_pcie_hotunplug`（device_del nvme0 → guest 移除事件）
  8. `test_ipmi_redfish_smoke`（Redfish GET /redfish/v1 → 200）

## 7. 性能回归方案

> 前提声明：TCG 非实时，**绝对性能不可信**；本方案只做**同配置下版本间相对回归**。

1. **确定性运行**：`-icount shift=auto,sleep=off`（guest 时钟确定，host 尽力快跑）。**实测约束**：`align=on` 与 `sleep=off` 互斥；`icount` 与 `-accel tcg,thread=multi`（MTTCG）互斥——确定性模式只能用单线程 TCG。
2. **指标定义**（perf_regression.py 采集，输出 JSON；**2026-08-19 实机首采成功**）：
   | 指标 | 采集方式 | 首采值（icount 确定性模式，4 核 TCG） |
   |---|---|---|
   | boot_to_login 墙钟 | 启动→login 提示符（同镜像同配置） | 210.0 s（宿主相关，仅回归参考） |
   | TB count（确定性工作量） | QMP `x-query-jit`（返回键为 `human-readable-text`）解析 `TB count` | 535,572 |
   | gen code size（翻译代码量） | 同上 `gen code size` | 296,000,879 B |
   | Redfish 响应延迟 | `-netdev user,hostfwd=tcp::2443-:443` + 控制平面 curl 计时（p50/p95） | null（BMC 网卡需 fdt workaround 才有 IP，环境项，容忍 null） |
   | I2C 事务延迟 | `-trace enable=aspeed_i2c_*` 时间戳分析（可脚本化） | 待接入 |
   | IPMI 延迟（可选） | guest 内 ipmitool 循环 + 时间戳（或 SSIF over I2C trace） | 待接入 |
   - 注：本版本 x-query-jit 无 `executed` 指令计数，改用确定性更强的 TB count / gen code size；Linux 构建可另用 TCG 插件（`contrib/plugins/`，本版本无 `insn` 插件，用 `execlog`/`ips` 等替代）。
3. **基线管理**：`perf_regression.py --baseline baseline.json` 与当前结果对比，超阈值（如 boot +10%、insn +5%）即 CI 失败；baseline 由每次发版更新。
4. **粒度**：按用例拆分（启动/传感器轮询/Redfish/存储 IO），避免单一大指标掩盖回归。

## 8. 分期路线图

| 阶段 | 内容 | 产出 |
|---|---|---|
| **M1 功能冒烟**（✅ 骨架已落地并验证） | launch 脚本 + QMP 客户端 + boot-to-login + 传感器/网络/存储基础用例 | faultinject/ 全套骨架；**控制平面 9/9 集成检查通过**（tests/verify_control_plane.py，fake-QMP 服务器验证） |
| **M2 注错框架**（🔨 进行中） | 五类注错目录用例化；QMP 补丁落地 | **P1 已取消**（主线 HMP `pcie_aer_inject_error` 可直接用）；**P2 LTPI 链路故障属性已实现**（hw/misc/aspeed_ltpi.c）；待编译验证 |
| **M3 性能回归**（✅ 已落地） | -icount + x-query-jit 确定性指标 + CI 基线门禁 | perf_regression.py + `faultinject/baseline.json` + CI 流水线（`faultinject/ci/`） |
| **M4 深度带外**（🔨 部分完成） | PECI host、LTPI 链路故障、双 QEMU 联动（x86 host guest + BMC 互联） | P2 LTPI ✅、P4 PECI（设备级+AST2700 接线）✅；双 QEMU 联动为后续项 |

### 实现状态（2026-08-19 轮次）

- ✅ 控制平面 `faultinject/qmp_client.py`（含 `tcp:`/`unix:` 双地址、AER/LTPI/SDMC/fan/PECI 助手）已通过 `faultinject/tests/verify_control_plane.py` 9 项集成检查（便携 Python 3.13.1，工作区 `.tools/py/`）
- ✅ **QEMU master 本机编译成功**（MSYS2 mingw64 + gcc 16.2.0，build-mingw/，`qemu-system-aarch64.exe`）
- ✅ **真实 AST2700 A2 平台启动 OpenBMC SDK v11.03 至 login**：vbootrom(caliptra) → BL31 → OP-TEE 4.9 → U-Boot 2023.10（识别 `SOC: AST2700-A2`）→ Phosphor OpenBMC 全服务
- ✅ **实机故障注入 15/16 + 存储/带外补充验证**（`live_fault_smoke.py` 等）：tmp105 温度、adm1272 掉电、set_link、**LTPI（P2）**、**SDMC ECC（P3）**、**max31785 风扇（P5）**、watchdog、**AER** 实机全过；**PECI（P4）** 设备级验证（ast2600）；**device_del 热拔**实机过；唯一缺口 `inject-nmi` 为机器级限制（Cortex-A35 无 FEAT_NMI）
- ✅ 本地补丁 P1–P5 全部落地（P1 解析为无需补丁）：登记于 `faultinject/patches/README.md`；**独立补丁文件 `faultinject/patches/qemu-master-local.patch`（`git apply` 可用，reverse-check 校验通过）**
- ✅ **P4 已在 AST2700 SoC 接线并验证**：PECI 基址 0x14C1F000 来自 AST2700 A2 Datasheet V1.2（用户芯片资料目录）与 AspeedTech 内核 dts（`peci-controller@14c1f000`，irq intc1_5 bit4 = GIC197）；`/machine/soc/peci` 属性 set/get 实测通过
- ✅ **Linux CI 集成已落地**：`faultinject/ci/run_ci_linux.sh`（通用入口）+ `.github/workflows/bmc-qemu-ci.yml`（GitHub Actions）+ `faultinject/ci/README.md`；流程 = 构建(含补丁) → 控制平面检查 → AST2700 启动 + pytest → perf 回归门禁（`--baseline faultinject/baseline.json`，超 +10% 失败）
- ✅ 性能回归实跑成功：`boot_wall_s=210s`、`tb_count=535572`、`gen_code_size=296MB`（`faultinject/baseline.json`）
- 🔨 后续增强（可选）：Redfish 延迟打通（PCIe2 fdt workaround）；存储 werror/truncate 完整用例在 CI 的 pytest 中验证（`test_bmc_functional.py` 已含）

## 9. 风险与限制（务必知悉）

1. **TCG 性能非实时**——绝对性能基准需实机/KVM 辅助；回归对比可用。
2. **AST2700 无 KCS/eSPI**（SLI/LTPI 取代）——KCS 类 IPMI 通路测试无法在 QEMU 覆盖（白皮书 §7.2），需实机/FPGA。
3. **OpenBMC 主线镜像不支持 AST2700**——固件依赖 AspeedTech fork（v11.03）；Linux 上游 aspeed-g7 尚未合入（v9 系列 2026-06 仍评审）。
4. **PCIe2 需要官方 workaround**（U-Boot 内 fdt 补丁 + 预置镜像）——测试脚本需内置该步骤或仅用 pcie.0/1。
5. 本地补丁 P1–P5 为自研，维护成本与上游同步需纳入计划；建议提交上游（尤其 P1 AER QMP 社区已多次需求）。
6. `-icount` 与 `-snapshot`/MTTCG 组合存在历史坑，若遇异常先摘除 `-icount` 复测。

## 10. 快速开始

```bash
# 1) 编译 QEMU master（含 TCG 插件，供性能回归）
cd qemu-master && mkdir build && cd build
../configure --target-list=aarch64-softmmu,x86_64-softmmu \
             --enable-modules --enable-slirp --enable-plugins \
             --disable-werror
ninja -j$(nproc)

# 2) 下载固件（AspeedTech SDK v11.03 的 ast2700-default）
#    https://github.com/AspeedTech-BMC/openbmc/releases/tag/v11.03

# 3) 启动被测平台（部件拓扑见 §3）
QEMU=./qemu-master/build/qemu-system-aarch64 \
IMG=<解压后的 image-bmc> bash faultinject/launch_ast2700.sh

# 4) 功能 + 注错用例
python3 -m pytest faultinject/test_bmc_functional.py -v

# 5) 性能回归
python3 faultinject/perf_regression.py --qmp /tmp/ast2700-qmp.sock \
                                       --baseline baseline.json --out result.json
```
