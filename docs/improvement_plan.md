# SchedX-Agent 完善计划

## 题目要求 vs 当前实现对比

### 评分维度对照

| 评分项 (分值) | 当前状态 | 得分预估 |
|---|---|---|
| **创新性 (30分)** | 架构有模块化设计思路，但Agent无决策循环，策略是静态规则 | ~12-15分 |
| **功能完整性 (25分)** | workload感知和cgroup控制完整，但scx/eBPF全部缺失 | ~10-13分 |
| **性能提升 (25分)** | 基准测试框架完整，有openEuler实际数据(README中)，但仓库内无完整结果 | ~12-15分 |
| **代码质量 (10分)** | 结构清晰、有测试(9个)、有dry-run、有安全保护 | ~7-8分 |
| **演示效果 (10分)** | CLI交互完整，但关键功能(scx)无法演示 | ~5-6分 |

**预估总分: 46-57/100**

---

### 逐项对比题目要求

#### 1. "设计并实现标准化的工具和Skills能力接口" — 部分完成

- `Skill` Protocol 已定义，但只实现了 ProbeSkill 和 AnalyzeSkill 两个
- PolicySkill、ActSkill、VerifySkill、RollbackSkill、ReportSkill 均缺失
- **关键问题**: main.py 中的 cmd_optimize 直接实例化各组件，绕过了 Skill 接口，Skills 形同虚设

#### 2. "Agent需要能够感知workloads" — 部分完成

- 已实现: procfs 探测(/proc)、PSI 压力探测、cgroup 状态探测
- 已实现: 基于规则的 workload 分类(latency_sensitive/background_noise/batch_compute)
- **缺失**: 无时序分析、无自适应阈值、无 I/O 和内存模式分析、分类规则全部硬编码

#### 3. "基于sched_ext调整CPU调度策略" — 未实现

- ScxController 仅检测 `/sys/kernel/sched_ext` 是否存在
- `scx_agent.bpf.c` 和 `scx_agent_user.c` 是空文件占位符
- `SafeActionExecutor` 对 scx 相关 Action 标记为 `skipped_phase1`
- **完全没有 BPF 程序、没有 libbpf/BCC 调用、没有 BPF map 通信**

#### 4. "集成scx调度器，实现workload性能优化" — 未实现

- 无 scx 调度器编译、加载、运行的任何代码
- allowlist 硬编码了 scx_simple/scx_rusty/scx_agent 但无法实际调用

#### 5. "支持扩展eBPF作为hook，实现network/security/resource agent" — 未实现

- `ebpf_probe.py` 全部是 mock stub，返回 `{"available": False}`
- 无任何 tracepoint 挂载、BPF map、网络/安全策略 hook
- 四个 Agent 类型(NetworkPolicy/SecurityPolicy/ResourceControl/SchedulerTrace)全部空壳

#### 6. "提供完整的性能对比测试数据和可复现的实验环境" — 部分完成

- 基准测试框架设计良好: 三阶段实验(baseline/interference/schedx)
- README 中有 openEuler 实测数据(62.56% RPS 下降, 2.36% 恢复, 14.83% 延迟降低)
- 脚本(check_env/install_deps/install_wrk/run_nginx_experiment)齐全
- **但仓库内 results/ 目录数据不完整**, 显示 "tool_missing" 和 "plan_only"

---

### 项目强项

1. **cgroup v2 控制** — 完整实现，支持 cpu.weight/cpu.max、PID 迁移、回滚、清理
2. **安全模型** — 干跑模式、受保护进程白名单、异常自动回滚、路径遍历防护
3. **基准测试框架** — 三阶段实验设计合理，报告生成完善
4. **代码质量** — 模块化清晰，9个测试文件覆盖核心功能

### 主要差距 (按优先级)

1. **sched_ext/scx 集成** — 题目核心要求，当前为零。需要实现 BPF 程序和用户态调度器
2. **eBPF hook 扩展** — 题目明确要求的扩展能力，当前全为 mock
3. **Agent 决策循环** — 当前是线性管道，缺少反馈环和动态调整
4. **Skills 接口未串联** — 定义了但没用起来，需要把 CLI 命令改为通过 Skill 管道执行
5. **分类器智能化** — 纯硬编码规则，缺少自适应能力
6. **仓库内测试数据** — 需要在 openEuler 上跑出完整数据并提交

---

## 完善计划 (5个阶段)

### 第一阶段：补全 Skills 接口 + Agent 决策循环 (对应"创新性"+ "功能完整性")

这阶段改的是架构，不动底层系统，纯 Python 可以在 Windows 上开发和测试。

1. **补全 5 个缺失的 Skill**: PolicySkill、ActSkill、VerifySkill、RollbackSkill、ReportSkill
2. **重构 main.py**: 所有 CLI 命令改为通过 Skill 管道执行，不再绕过接口
3. **实现 Agent 决策循环**: 观察→决策→执行→验证→反馈的闭环，支持动态策略调整
4. **加入 AgentContext 状态管理**: 决策日志、历史记录、会话管理

**预估**: 可将创新性分从 12-15 提升到 20-25

### 第二阶段：实现 sched_ext/scx 集成 (对应"功能完整性"核心)

这是题目核心要求，分两步走：

1. **实现 scx_agent BPF 程序** (`scx_agent.bpf.c`): 基于 sched_ext 的简单调度器，实现 dispatch 回调
2. **实现用户态加载器** (`scx_agent_user.c`): 通过 libbpf 加载 BPF 程序，通过 BPF map 传递调度参数
3. **完善 ScxController**: 实现真实的 scheduler 启停、参数调整、状态监控
4. **实现 PolicyPlanner 到 scx 的映射**: workload 分类结果 → scx 调度参数调整

**预估**: 可将功能完整性从 10-13 提升到 18-22

### 第三阶段：实现 eBPF hook 扩展 (对应"功能完整性"扩展要求)

1. **sched_trace BPF 程序**: 挂载 sched_switch/sched_wakeup tracepoint，采集调度延迟数据
2. **网络策略 hook**: 基于 BPF tc/XDP 的简单网络策略控制
3. **资源控制 hook**: 基于 BPF 的 cgroup 操作增强
4. **将 ebpf_probe.py 从 mock 改为真实实现**: 通过 libbpf/BCC 加载和管理 BPF 程序

**预估**: 可将功能完整性再提升 3-5 分

### 第四阶段：增强分类器 + 性能优化闭环 (对应"性能提升")

1. **改进 WorkloadClassifier**: 加入调度延迟、内存压力、I/O 等多维度特征
2. **实现自适应阈值**: 基于历史数据动态调整分类边界
3. **实现优化→验证→再优化循环**: Agent 执行优化后自动运行 benchmark 验证效果，效果不佳则调整策略
4. **在 openEuler 上跑完整基准测试**: 补充 redis-latency、batch-cpu 场景的数据

**预估**: 可将性能提升分从 12-15 提升到 18-22

### 第五阶段：收尾打磨 (对应"代码质量"+ "演示效果")

1. **补充测试**: 为新增的 Skill、Agent 循环、BPF 集成写单元测试
2. **完善文档**: 技术报告、架构图、API 文档
3. **准备演示脚本**: 一键跑通 "感知→分类→优化→验证→报告" 全流程
4. **收集完整测试数据**: 在 openEuler 上跑通所有场景，结果提交到仓库

**预估**: 代码质量 8-9 分，演示效果 8-9 分

---

## 各阶段总结

| 阶段 | 重点 | 改动范围 | 难度 |
|---|---|---|---|
| 1 | Skills + Agent 循环 | 纯 Python，可 Windows 开发 | 中 |
| 2 | sched_ext/scx | C + BPF + Python，需 openEuler 编译 | 高 |
| 3 | eBPF hooks | C + BPF + Python，需 openEuler 编译 | 高 |
| 4 | 分类器 + 性能闭环 | Python + benchmark | 中 |
| 5 | 测试 + 文档 + 演示 | 全栈 | 低 |
