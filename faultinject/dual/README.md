# 双 QEMU 联动（x86 host guest + BMC）— 路线图 M4

## 场景

host CPU/DIMM 故障（MCE）须经带外通道（PECI / IPMB）被 BMC 感知：
```
qemu-system-x86_64 (host guest)            qemu-system-aarch64 (BMC DUT)
  ├─ IPMI BMC 仿真 (ipmi-bmc-sim)   ...?    ├─ ast2700-evb + OpenBMC
  ├─ HMP `mce` 注入（CPU/DIMM 故障）         └─ PECI (patch P4) / LTPI (P2)
  └─ QMP :4460                              └─ QMP :4461
        │                                        │
        └────── QMP-to-QMP 桥（start_dual.py 骨架）──────┘
              host MCE → bmc PECI host-lost/temp-fault
```

## 现状（骨架已提供）

- `start_dual.py`：启动双实例、双 QMP 连接、`bridge_host_mce_to_bmc()`（host MCE → BMC PECI 故障属性）。
- host 侧 MCE 用 QEMU 现成 HMP `mce`（hmp-commands.hx:1511）；IPMI 仿真用 `ipmi-bmc-sim + isa-ipmi-kcs`。

## 待实现（真实线协议桥）

1. **IPMB-over-socket**：把 host guest 的 IPMI 请求（QEMU ipmi-bmc-sim 为出口）转发给 BMC guest 的 ipmid（SSIF over I2C 或 BT）——需要外部桥进程 + 双方串口/套接字接线。
2. **PECI 线协议**：当前 P4 是寄存器级故障注入；真实 PECI GetTemp/RdPkgConfig 应答需在 aspeed_peci 中按命令解析（数据来自 host 侧 QMP 遥测）。
3. **验证口径**：host 注入 MCE 后，BMC 的 host-status 服务（如 phosphor-host-ipmid / peci 轮询）应产生告警日志。

## 验证方式（骨架级）

```bash
HOST_IMG=host.qcow2 BMC_IMG=images/ast2700-default-image/image-bmc \
python3 faultinject/dual/start_dual.py
# 预期输出：host MCE 注入 OK、BMC PECI temp-fault 置位/清除 OK、双实例 running
```
