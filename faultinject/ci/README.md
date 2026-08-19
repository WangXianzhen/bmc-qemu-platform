# Linux CI 集成说明

将 BMC 验证平台接入 Linux CI（GitHub Actions / Jenkins / GitLab CI 通用）：
构建 QEMU master + 本地注错补丁 → 控制平面检查 → AST2700 启动 + 功能/注错 pytest → 性能回归门禁。

## 组成

| 文件 | 作用 |
|---|---|
| `run_ci_linux.sh` | 通用入口：apt 依赖、拉取 QEMU + 应用 `patches/qemu-master-local.patch`、构建 aarch64-softmmu、下载 SDK v11.03 镜像、跑 `verify_control_plane.py` + pytest + perf 门禁 |
| `../../.github/workflows/bmc-qemu-ci.yml` | GitHub Actions 工作流（push/PR 触发，ubuntu-latest，60min 超时） |
| `../../faultinject/patches/qemu-master-local.patch` | 全部本地补丁（P2–P5、P4 AST2700 接线、Windows BUILD-FIX）的独立 patch 文件，`git apply` 应用 |

## 门禁逻辑（性能回归）

- `perf_regression.py --baseline <repo>/faultinject/baseline.json --out result.json`
- 任一指标相对 baseline 超 +10%（默认 `--tolerance 0.10`）→ 非零退出 → CI 失败
- 指标：`boot_wall_s`（宿主相关）、`tb_count` / `gen_code_size`（确定性工作量代理）
- 更新基线：`cp result.json faultinject/baseline.json` 后提交（每次有意变更镜像/配置时）

## 本地试跑（Linux）

```bash
bash faultinject/ci/run_ci_linux.sh            # 全流程
bash faultinject/ci/run_ci_linux.sh SKIP_PERF=1  # 跳过 perf（首次调试更快）
```

## 备注

- pytest 需要 `python3-pytest`（脚本已含）；`test_bmc_functional.py` 在 Linux 上经 `-nographic` stdin 交互（Windows 上无此通道，属预期）
- 镜像下载约 52MB（AspeedTech OpenBMC SDK v11.03 `ast2700-default-image.tar.gz`），可预置缓存到 `images/` 跳过重下
- 首次 CI 无 baseline 时 perf 步骤输出"no baseline"，以 `result.json` 为候选基线；提交 baseline 后即启用门禁
