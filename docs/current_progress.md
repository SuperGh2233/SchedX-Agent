# SchedX-Agent 当前进展

更新日期：2026-06-14

## 当前结论

项目已经完成赛题核心链路，并在 openEuler 24.03-LTS-SP3 与自编译
Linux `6.15.11-schedx` 内核上完成真实验证：

```text
workload / Agent tool call
  -> probe and intent
  -> Agent decision
  -> native sched_ext weighted-vtime policy
  -> cgroup v2 CPU/memory/pids enforcement
  -> metrics and feedback
  -> rollback and cleanup
```

## 已完成能力

- 标准化 Skills：Probe、Analyze、Policy、Act、Verify、Rollback、Report、scx、eBPF、LLM。
- workload 感知：进程、调度统计、PSI、cgroup、CPU 拓扑与规则分类。
- 自主决策闭环：感知、分类、策略、执行、验证、反馈与回滚。
- 原生 sched_ext：真实 `sched_ext_ops` 调度器、三类 DSQ、动态 PID 策略和同类任务加权 vtime。
- Agent 工具调用管控：按工具语义创建临时层级 cgroup，执行 CPU/内存/PID 约束并反馈资源压力。
- 标准化意图协议：`AGENT_RESOURCE_HINT=intent:compile,memory:high,cpu:high`。
- 自适应反馈闭环：将 memory pressure、OOM 和 CPU throttling 转换为下一轮可复用资源意图。
- 常驻 scx 管理守护进程：多个 Agent 通过 Unix Socket 共享唯一 struct_ops 调度器。
- cgroup 级策略继承：子进程和孙进程无需逐 PID 注册即可继承调度类别与权重。
- 自适应公平性：根据活跃类别与 CPU PSI 动态设置后台服务下限，避免严格优先级饥饿。
- cgroup 调度指标与闭环调参：按进程树采集运行/等待时间，并依据后台实际运行份额自动调节。
- DeepSeek V4 策略 Agent：真实模型参与决策，结构化提案经过本地安全校验后进入执行闭环。
- 安全降级：不支持 sched_ext 或 struct_ops 已被占用时，继续使用 cgroup v2 管控。
- 可复现实验：默认调度器对比、原生 sched_ext 对比、Agent 工具调用受管控对比。

## 真实验证结果

- Windows 本地测试：`63 passed, 4 skipped`；openEuler 测试：`67 passed`。
- VM 内核：`6.15.11-schedx`。
- sched_ext 状态：调度器运行后可安全卸载，`nr_rejected=0`。
- 原生 sched_ext 正式实验：RPS 提升 `182.67%`，P99 降低 `99.58%`。
- Agent 工具调用正式实验：平均延迟降低 `17.16%`，10/10 次启用原生 sched_ext。
- Agent 工具调用实验后台 CPU 保留率：`84.78%`。
- memory pressure 场景可生成 Agent 可操作的降并行度/拆分任务反馈。
- 实测压力场景自动生成 `[schedx-next-hint] intent:test,memory:high`。
- 8 个并发 Agent 工具调用全部使用 daemon 原生 sched_ext；策略结束后归零，死亡 PID 策略可自动回收。
- 自适应正式实验：RPS 提升 `87.89%`，P99 降低 `17.58%`，后台 CPU 保留率 `18.74%`。
- cgroup 继承实测：BPF map 中仅有 cgroup policy、无 task policy，进程树仍正确进入对应调度类别。
- 闭环收敛实测：控制器依据运行份额将服务间隔从 `4096` 逐步调整至 `512`。
- 指标闭环正式实验：RPS 提升 `88.29%`，P99 降低 `28.16%`，后台 CPU 保留率 `17.29%`。
- DeepSeek V4-Pro 实测：真实 API 调用生成 `latency_first / redis-server` 策略，完整 dry-run Agent 链通过。

## 对赛题要求的覆盖

| 赛题要求 | 当前实现与证据 | 状态 |
| --- | --- | --- |
| 标准化工具和 Skills 接口 | Skills、AgentLoop、CLI、工具调用意图协议 | 已完成 |
| 感知 workloads | procfs、PSI、cgroup、调度统计、语义意图 | 已完成 |
| 基于 sched_ext 调整策略 | 原生 struct_ops、分类 DSQ、加权 vtime、动态 PID map | 已完成 |
| 集成 scx 并优化性能 | `results/native-scx-formal/` | 已完成 |
| 扩展 eBPF/cgroup Agent | eBPF hooks、cgroup 控制、Agent 工具调用资源管控 | 已完成核心能力 |
| 完整性能数据与可复现环境 | 安装文档、实验脚本、JSON 与 Markdown 报告 | 已完成 |
| openEuler 编译运行测试 | openEuler 24.03-LTS-SP3 VM 实测 | 已完成 |

## 当前主要风险

- 自适应公平性已避免后台严重饥饿，但不同 workload 的最优服务间隔仍需要更多实验数据。
- 守护进程目前使用本地 Unix Socket 与 systemd 管理；跨主机调度不属于当前赛题范围。
- 当前目录不是 Git 工作区，无法提供完整的代码提交历史，这是开发过程评分风险。
- network/security Agent 的 eBPF 扩展能力已有基础，但不应在答辩中宣称为主完成项。

## 下一步优先级

1. 制作一键演示脚本，串联状态检查、并发调度、继承验证、压力反馈和报告生成。
2. 扩展 Redis、编译和多 Agent 场景的公平性实验矩阵。
3. 整理技术报告与答辩材料，明确创新点、实验方法、收益边界和降级路径。
