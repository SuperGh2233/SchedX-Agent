# SchedX-Agent 当前进展

更新时间：2026-07-18

## 当前结论

SchedX-Agent 已在 openEuler 24.03 LTS SP4 上形成真实的资源管控闭环：

```text
workload 感知
  -> 规则/LLM 结构化决策
  -> 真实 SLO canary baseline
  -> native sched_ext + cgroup v2 执行
  -> candidate SLO 与后台进度验证
  -> 接受策略或自动 rollback
```

## 已完成能力

- 标准化 Skills：Probe、Analyze、Policy、Act、Verify、Rollback、Report、scx、eBPF 和 LLM。
- workload 感知：procfs、进程调度信息、PSI、cgroup 指标和规则分类。
- 安全执行：结构化 Action、命令白名单、显式 dry-run、系统进程保护和回滚记录。
- cgroup v2：CPU weight/max、cpuset、PID 迁移、状态恢复和空目录清理。
- 原生 sched_ext：自定义 `sched_ext_ops`、三类 DSQ、task/cgroup policy map 和 weighted-vtime。
- 持久 scx daemon：通过 Unix Socket 串行管理唯一 struct_ops owner 和并发 Agent 策略。
- 公平性控制：运行时与正式实验统一从 background interval `64` 开始，按运行份额闭环调整。
- 真实 SLO canary：动作前后调用 wrk，比较 RPS/P99、后台 CPU 进度和 `nr_rejected`。
- 自动回滚：canary 拒绝后清除 cgroup 设置和持久 scx task policy。
- 可复现实验：nginx 混合负载、原生 scx、Agent tool-call、daemon 并发与权重验证。

## SP4 验证结果

- 用户态：openEuler 24.03 LTS SP4。
- stock 内核：`6.6.0-159.4.3.154.oe2403sp4`，未启用 `CONFIG_SCHED_CLASS_EXT`。
- native scx 内核：`6.6.0-159.4.3.154.oe2403sp4.schedx1`。
- 定制内核配置：`CONFIG_SCHED_CLASS_EXT=y`、`CONFIG_BPF_SYSCALL=y`、`CONFIG_DEBUG_INFO_BTF=y`。
- 虚拟机测试：`125 passed`，`nr_rejected=0`。
- 同类任务权重验证：约 `8.1:1`，最低门槛 `5:1`。
- 最新公平性正式实验：3 次重复、每次 10 秒。
- 平均 RPS：`66947.32 -> 108575.78`，提升 `62.18%`。
- 平均 P99：`6.85 ms -> 4.54 ms`，降低 `33.74%`。
- 后台 CPU 保留率：`32.22%`，最低有效门槛 `25%`。
- 真实 canary 接受路径：RPS 提升 `13.32%`、P99 降低 `92.82%`、后台进度保留 `81.86%`。
- 真实 canary 拒绝路径：后台进度保留 `77.44%`，在 `100%` 安全门槛下触发拒绝。
- 拒绝后证据：恢复 `15` 项 cgroup 设置、删除 `6` 个 cgroup、清除全部持久 scx task policy，最终无 `/sys/fs/cgroup/schedx` 和 `.schedx/scx_rollback.json` 残留。
- 正式四组消融：`default / cgroup-only / scx-only / agent-combined`，每组 20 秒、3 次重复。
- `agent-combined` 相对 default：RPS 提升 `86.66%`、P99 降低 `72.33%`、后台进度保留 `43.92%`。
- sysbench 正式实验：干扰使吞吐降低 `47.47%`，SchedX 相对干扰组恢复 `70.51%`。
- 一键正式实验目录：`results/competition-demo/2026-07-16_07-38-05/`。
- 自动比赛报告：`reports/competition-final.md`。

正式结果目录：

```text
results/native-scx/fairness-gated-formal-20260716-010758/
```

## 对赛题要求的覆盖

| 赛题要求 | 当前实现 | 状态 |
| --- | --- | --- |
| 标准化工具和 Skills | AgentContext、Skills、CLI、结构化 Action | 已完成 |
| 感知 workloads | procfs、sched、PSI、cgroup 和规则分类 | 已完成 |
| 基于 sched_ext 调整策略 | 原生 struct_ops、分类 DSQ、加权 vtime、动态 map | 已完成 |
| 集成 scx 优化性能 | 公平性门控的 default/native 对比 | 已完成主场景 |
| cgroup resource-control Agent | CPU/cpuset 控制、rollback、tool-call cgroup | 已完成 |
| eBPF 扩展 hook | 接口和加载框架存在，独立 trace/network/security hook 尚未真实 attach | 部分完成 |
| 性能数据和复现环境 | 原始 wrk、JSON、报告、SP4 内核构建与回退脚本 | 持续完善 |
| openEuler SP4 运行测试 | stock fallback 与定制内核 native scx | 已验证 |

## 当前风险

- eBPF network/security hook 仍是扩展接口，答辩时不能宣称已经完整实现。
- 当前正式性能数据覆盖 nginx 和 sysbench；Redis 仍可作为补充场景。
- wrk、nginx 和干扰负载同机运行，后续应增加独立压测端以降低客户端竞争偏差。
- policy repository 已能保存结果，但还需要更多真实 outcome 才能支撑学习型路由结论。

## 下一优先级

1. 增加 Redis、sysbench 和独立压测机实验矩阵。
2. 实现一个真实 sched_wakeup/sched_switch eBPF 延迟 trace hook。
3. 扩充 policy repository 的多场景 outcome，并验证路由切换稳定性。
4. 将 native scx、公平性与 canary 证据整合进比赛一键演示脚本。
