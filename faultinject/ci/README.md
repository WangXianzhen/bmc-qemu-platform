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

## 推送到 GitHub（CI 自动生效）

本地仓库已提交（`git log`）。推送后 `.github/workflows/bmc-qemu-ci.yml` 会在 push/PR 时自动运行全流程。

```bash
# 1) GitHub 上新建空仓库（不要勾选 README/.gitignore 初始化），例如
#    github.com/<you>/bmc-qemu-platform
# 2) 本地关联并推送（用 PAT 或 gh auth login 完成认证）
cd <本仓库根目录>
git remote add origin https://github.com/<you>/bmc-qemu-platform.git
git push -u origin main

# 3) 查看 CI：GitHub → Actions → BMC-QEMU-CI（首次运行约 15–20 分钟：
#    构建 QEMU + 补丁 → 控制平面检查 → AST2700 启动 + pytest → 性能门禁）
```

- 若本机已有 `gh` CLI 认证：`gh repo create bmc-qemu-platform --public --source . --push`
- 仓库不包含 QEMU 源码/固件镜像/本地工具（`qemu-master/`、`images/`、`.tools/` 已 gitignore）——CI 自行克隆 QEMU 并 `git apply` 补丁、下载镜像
- 首次 CI 的 perf 步骤以仓库内 `faultinject/baseline.json` 为基线；有意变更镜像/配置后 `bash faultinject/ci/update_baseline.sh` 并提交新基线

## 本地试跑（Linux）

```bash
bash faultinject/ci/run_ci_linux.sh              # 全流程（构建→检查→pytest→perf 门禁）
bash faultinject/ci/run_ci_linux.sh SKIP_PERF=1  # 跳过 perf（首次调试更快）
```

## 备注

- pytest 需要 `python3-pytest`（脚本已含）；`test_bmc_functional.py` 在 Linux 上经 `-nographic` stdin 交互（Windows 上无此通道，属预期）
- 镜像下载约 52MB（AspeedTech OpenBMC SDK v11.03 `ast2700-default-image.tar.gz`），可预置缓存到 `images/` 跳过重下
- 首次 CI 无 baseline 时 perf 步骤输出"no baseline"，以 `result.json` 为候选基线；提交 baseline 后即启用门禁
