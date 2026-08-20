# 双 QEMU 联动（x86 host guest + BMC）— 路线图 M4

## 场景

host CPU/DIMM 故障（MCE）须经带外通道（PECI / IPMB）被 BMC 感知：
```
qemu-system-x86_64 (host guest)            qemu-system-aarch64 (BMC DUT)
  ├─ ipmi-bmc-extern（外部 BMC 协议）  ←→  ipmb_bridge.py（IPMB 桥）
  │       │                                  ├─ ast2700-evb + OpenBMC
  ├─ HMP `mce` 注入（CPU/DIMM 故障）          └─ PECI (patch P4) / LTPI (P2)
  └─ QMP :4460                                └─ QMP :4461
```

## 现状（已实现）

- `ipmb_bridge.py`：**完整的 VM 协议桥**（与 hw/ipmi/ipmi_bmc_extern.c 逐字节一致）：
  - 组帧/转义（0xA0/0xA1/0xAA → 0xAA+位4）、IPMB 校验和、流式解码（msg/command）
  - `--mock`：内置 IPMI 应答（Get Device ID / Get Self Test / Chassis Status…）
  - `--forward SOCK`：把请求转发到后端 socket（如 BMC ipmid 的 unix socket）
  - `--self-test`：7 项协议向量自测（**已通过**，并在 CI 中执行）
- `start_dual.py`：host guest 改用 `ipmi-bmc-extern` + 桥（mock 模式），双 QMP 连接 + `bridge_host_mce_to_bmc()`

## 待实现（真实 BMC 后端）

1. **forward 模式接 BMC ipmid**：把 `--forward` 指向 BMC guest 的 ipmid socket 运输层
   （需确认 SDK 镜像 ipmid 的 socket 支持；否则需 SSIF-over-I2C 或自定义转发）
2. **host 侧 guest 验证**：需一枚 x86 guest 镜像（HOST_IMG），boot 后 `ipmitool mc info`
   应看到桥应答的 BMC（Get Device ID 0x00），并验证 KCS 通路
3. **IPMB-over-socket 线协议**（OpenIPMI lanplus/IPMB 帧）替代当前 mock 应答

## 验证方式

```bash
python3 faultinject/dual/ipmb_bridge.py --self-test     # 协议自测（CI 已含）
HOST_IMG=host.qcow2 BMC_IMG=images/ast2700-default-image/image-bmc \
python3 faultinject/dual/start_dual.py                  # 双实例 + 桥 + MCE 桥接
```
