# AST2700 BMC 验证平台（QEMU）

基于 QEMU master（AST2700 A2）的 **BMC 功能 / 性能 / 异常注错验证平台**：BMC 固件（OpenBMC SDK v11.03）为被测体，被管理平台部件（传感器/PSU/风扇/PCIe/存储/带外通道）由 QEMU 仿真，经 QMP 控制平面注入故障；性能以 `-icount` + `x-query-jit` 确定性指标做版本回归。

## 目录

| 路径 | 内容 |
|---|---|
| `faultinject/` | 控制平面：`qmp_client.py`（QMP 客户端+注错助手）、`launch_ast2700.sh`（部件拓扑）、`fault_matrix.yaml`（五类注错目录）、`test_bmc_functional.py`（pytest 功能/注错套件）、`perf_regression.py`（性能回归） |
| `faultinject/patches/` | 本地 QEMU 补丁（P2–P5 + P4 AST2700 接线 + Windows 构建兼容），`qemu-master-local.patch` 供 CI `git apply` |
| `faultinject/ci/` | Linux CI 集成：`run_ci_linux.sh`（构建→控制平面检查→pytest→性能门禁）、`update_baseline.sh`、`README.md` |
| `faultinject/tests/` | `verify_control_plane.py`（fake-QMP 集成测试 9/9）、`live_fault_smoke.py`（实机注错 15/16）、`verify_p4_peci.py` |
| `.github/workflows/bmc-qemu-ci.yml` | GitHub Actions：push/PR 触发全流程 CI |
| `BMC验证平台-设计方案.md` | 完整设计文档（架构/拓扑/注错矩阵/补丁清单/性能方法论/路线图） |

## 快速开始

```bash
# 构建（本机已验证：MSYS2 mingw64 / Linux）
bash faultinject/ci/run_ci_linux.sh          # Linux 全流程（含 CI 门禁）

# 手动启动被测平台
QEMU=./build-mingw/qemu-system-aarch64.exe \
IMG=images/ast2700-default-image/image-bmc \
bash faultinject/launch_ast2700.sh

# 实机注错冒烟
python3 faultinject/tests/live_fault_smoke.py
```

## CI 门禁与基线

- 每次 push/PR：构建 QEMU+补丁 → 控制平面检查 → AST2700 启动 + pytest 功能/注错 → `perf_regression.py --baseline faultinject/baseline.json`（任一指标超 +10% 即失败）
- **有意变更镜像/固件配置后更新基线**：`bash faultinject/ci/update_baseline.sh`（或 `cp result.json faultinject/baseline.json`）后提交

## 已知限制

- `inject-nmi` 在 AST2700 不可用（Cortex-A35 无 FEAT_NMI，机器级限制，详见设计方案 §7）
- Redfish 延迟指标需 PCIe2 fdt workaround（BMC 网卡获 IP 后可用，pytest 中优雅跳过）
- 双 QEMU 联动（x86 host guest + BMC 互联）为路线图项，见设计方案 M4
