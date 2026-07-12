# SchedX-Agent 下一阶段开发分析与 Prompt

本文档用于分析当前 SchedX-Agent MVP 与“系统创新-面向 Linux 的自适应资源管控 Agent”社区赛题之间的差距，并给出下一阶段开发路线。最后一节提供一段可直接复制给 Codex、GPT-5.5、Cursor 等工具的下一步实现 Prompt。

## 1. 当前代码现状

当前仓库已经完成第一阶段 MVP，具备基础可运行框架：

- CLI 已实现 `schedx status/probe/classify/optimize/control/rollback/benchmark/report`，入口位于 `schedx/main.py`。
- `ProcfsProbe` 已采集进程 PID、命令名、CPU 占用、RSS、上下文切换和 PSI 数据，位于 `schedx/probes/procfs_probe.py`。
- `WorkloadClassifier` 已提供静态规则分类，支持 `latency_sensitive`、`batch_compute`、`background_noise`、`mixed` 等类型，位于 `schedx/policies/classifier.py`。
- `PolicyPlanner` 已能生成结构化 action，避免 Agent 直接执行任意 shell 命令。
- `CgroupController` 已支持 cgroup v2 基础操作和 rollback 记录，包括创建 cgroup、移动 PID、设置 `cpu.weight`、设置 `cpu.max`，位于 `schedx/controllers/cgroup_controller.py`。
- `ScxController` 已能检测 `/sys/kernel/sched_ext`、读取 state 和当前 scheduler 状态，位于 `schedx/controllers/scx_controller.py`。
- `BenchmarkRunner` 和 `ReportGenerator` 已有基础框架，但当前 benchmark 仍是 `plan_only` 占位。
- `scx/scx_agent.bpf.c`、`scx/scx_agent_user.c` 和 `schedx/probes/ebpf_probe.py` 目前是 Phase 2 占位或 mock。

整体判断：当前项目已经具备“可运行 MVP”和安全控制骨架，但距离比赛高分仍缺少真实 openEuler 实验、真实 scx 调度器集成、eBPF hook 实现、可复现性能数据和更完整的 Skills 闭环。

## 2. 对照赛题要求的完成度分析

| 赛题要求 | 当前完成度 | 说明 |
| --- | --- | --- |
| 标准化工具和 Skills 接口 | 部分完成 | 已有 `Skill`、`SkillResult`、`ProbeSkill`、`AnalyzeSkill`，但 `PolicySkill`、`ActSkill`、`VerifySkill`、`RollbackSkill`、`ReportSkill` 还未形成完整闭环。 |
| workload 感知 | 部分完成 | 已采集 procfs 和 PSI 基础指标，但尚未读取 `/proc/[pid]/sched` 的运行队列等待、调度统计和 cgroup CPU 使用趋势。 |
| 基于 sched_ext 调整 CPU 调度策略 | 未完成 | `ScxController` 目前只有 detection，`start_scheduler/stop_scheduler` 仍是 `NotImplementedError`。 |
| 集成 scx 调度器优化 workload | 未完成 | `scx_agent.bpf.c` 和 `scx_agent_user.c` 仍是占位，尚未加载真实 scx 程序或接入系统已有 scx 调度器。 |
| eBPF hook 扩展能力 | 占位完成 | 已有 mock hook 接口，但没有真实 tracepoint、BPF map 或用户态事件读取。 |
| 性能对比测试数据 | 未完成 | `BenchmarkRunner` 目前是 `plan_only`，没有真实调用 `wrk`、`redis-benchmark`、`sysbench`。 |
| 可复现实验环境 | 部分完成 | 已有 openEuler 安装文档和实验脚本雏形，但脚本还不能自动产出 before/after 性能结果。 |
| openEuler 24.03-LTS-SP3 编译运行测试 | 待验证 | 当前主要在 Windows 工作区验证 Python 命令降级运行，仍需在 openEuler 上 root 权限实测 cgroup 和 benchmark。 |

## 3. 当前主要短板

1. 缺少真实性能数据。比赛评分中性能提升占 25 分，必须尽快跑通 nginx、redis、batch 三类 before/after 实验。
2. scx 集成仍停留在检测层。赛题核心是用户态调度和 sched_ext，必须至少支持白名单 scx loader，并给出可替换的自定义 `scx_agent` 方案。
3. workload 感知深度不足。当前分类主要依赖进程名和 CPU 占用，缺少 `/proc/[pid]/sched`、cgroup `cpu.stat`、PSI 趋势窗口等更有系统深度的指标。
4. cgroup 控制缺少 openEuler 实测。需要处理 cgroup v2 是否可写、subtree controller 是否启用、权限不足、PID 退出、回滚失败等情况。
5. benchmark/report 还不能支撑演示。当前 report 只汇总 JSON，没有 before/after 对比表、提升比例和图表。
6. Skills 闭环不完整。已有 Skill 接口，但 CLI 仍主要直接调用 probe/classifier/planner/controller，后续需要让 Probe、Analyze、Policy、Act、Verify、Rollback、Report 串成明确 Agent 流程。

## 4. 下一阶段优先级

按比赛得分收益和演示价值排序，下一阶段建议这样推进：

1. 完善可演示闭环：真实 benchmark + cgroup 策略优化 + rollback。先让项目能在没有 sched_ext 的普通 openEuler 环境中展示明确 before/after 数据。
2. 接入 scx 适配层：实现 whitelisted scx loader，优先支持系统已有 `scx_simple`、`scx_rusty` 或自定义 `scx_agent` 占位加载。
3. 扩展 workload 感知：读取 `/proc/[pid]/sched`、cgroup CPU 使用、PSI 趋势窗口，并为分类结果输出 reason。
4. 补充 eBPF hook：先实现 scheduler trace hook，再扩展 network/security/resource control 插件接口。
5. 强化报告生成：自动生成 before/after JSON、Markdown 表格、性能提升比例和可选 PNG 图表。

## 5. 具体完善方向

### 5.1 Benchmark 与实验闭环

- 将 `schedx/benchmark/runner.py` 从 `plan_only` 改为真实 runner。
- 新增或拆分 nginx、redis、batch benchmark 模块。
- 使用白名单命令调用 `wrk`、`redis-benchmark`、`sysbench`，禁止任意 shell。
- 解析指标并输出标准 JSON：
  - `results/nginx_default.json`
  - `results/nginx_schedx.json`
  - `results/redis_default.json`
  - `results/redis_schedx.json`
  - `results/batch_default.json`
  - `results/batch_schedx.json`
- 实验脚本负责启动干扰负载、运行默认测试、执行 `schedx optimize --apply`、运行优化后测试、最后 rollback。

### 5.2 cgroup-only 优化能力

- `schedx optimize --target nginx --mode latency_first --apply` 应能找到 nginx PID，并移动到高权重 cgroup。
- 自动识别 `stress-ng`、`stress` 等 background workload，并移动到低权重 cgroup。
- 写入前记录 rollback；任一 action 失败时自动回滚已执行 action。
- 增加 cgroup v2 可写性检查和错误建议，例如提示使用 root、检查 `/sys/fs/cgroup/cgroup.controllers`、启用 `+cpu`。

### 5.3 workload 感知增强

- 在 `ProcfsProbe` 中读取 `/proc/[pid]/sched`，提取可用字段：
  - `nr_switches`
  - `se.sum_exec_runtime`
  - `se.statistics.wait_sum`
  - `se.statistics.wait_count`
- 读取 cgroup `cpu.stat`、`cpu.pressure`，形成 process 与 cgroup 的关联信息。
- 为分类结果输出 `reason`，例如 `comm=stress-ng and cpu_percent>50`。
- 增加短窗口采样能力，让策略可以根据 CPU pressure 趋势而不是单点值决策。

### 5.4 scx 适配层

- `ScxController.start_scheduler()` 只允许启动白名单程序，不接受任意 shell。
- 白名单建议先支持：
  - `scx_simple`
  - `scx_rusty`
  - `scx_agent`
- 若 `/sys/kernel/sched_ext` 不存在，则继续 fallback 到 cgroup-only 模式。
- 在 `scx/README.md` 中明确自定义 `scx_agent` 设计：
  - BPF map 以 PID 或 cgroup id 为 key。
  - 用户态 loader 从 SchedX action JSON 写入 workload class 和 weight。
  - 调度器对 latency-sensitive workload 使用更高权重或优先 dispatch queue。

### 5.5 eBPF hook 扩展

- 第一阶段真实 hook 建议从 `sched_switch` tracepoint 开始。
- 用户态读取事件后统计 workload 的切换频率、被抢占情况、运行/等待趋势。
- NetworkPolicyHook、SecurityPolicyHook、ResourceControlHook 先保持插件接口和 mock 数据，但文档中说明扩展点。

### 5.6 Report 与演示材料

- `schedx report` 自动读取 `results/*.json`。
- 生成 before/after 对比表。
- 计算提升比例：
  - p99 latency 降低百分比。
  - QPS 提升百分比。
  - batch 完成时间降低百分比。
- 如果 matplotlib 可用，生成：
  - `reports/figures/nginx_latency.png`
  - `reports/figures/redis_qps.png`
  - `reports/figures/batch_time.png`
- 如果 matplotlib 不可用，则只生成 Markdown 表格，不影响主流程。

## 6. 推荐下一阶段里程碑

### Milestone 1：cgroup-only 可演示闭环

目标：在没有 sched_ext 的 openEuler 环境中，也能展示 SchedX-Agent 的 workload 感知、策略决策、cgroup 执行、验证报告、rollback 闭环。

验收命令：

```bash
schedx status
schedx probe --top 10
schedx classify
sudo schedx optimize --target nginx --mode latency_first --apply
sudo schedx rollback --apply
```

### Milestone 2：真实 benchmark 数据

目标：产出 nginx、redis、batch 的 before/after JSON，并生成 Markdown 报告。

验收命令：

```bash
scripts/run_nginx_experiment.sh
scripts/run_redis_experiment.sh
scripts/run_batch_experiment.sh
schedx report
```

### Milestone 3：scx 可替换适配层

目标：在 sched_ext 可用环境中，能够通过白名单启动 scx 调度器；不可用时保持 cgroup-only fallback。

验收命令：

```bash
schedx status
sudo schedx optimize --target nginx --mode latency_first --apply
```

### Milestone 4：eBPF hook 与系统深度展示

目标：至少实现一个真实 scheduler trace hook，并在报告中展示调度事件数据。

验收命令：

```bash
schedx probe --with-ebpf
schedx report
```

## 7. 下一步可直接使用的 Prompt

```markdown
请基于当前 SchedX-Agent MVP 代码继续实现第二阶段，目标是让项目更符合“系统创新-面向 Linux 的自适应资源管控 Agent”比赛要求，并优先形成可演示、可测量、可回滚的闭环。

当前仓库已有：
- CLI：`schedx status/probe/classify/optimize/control/rollback/benchmark/report`
- procfs/PSI 探测：`schedx/probes/procfs_probe.py`
- workload 分类：`schedx/policies/classifier.py`
- cgroup v2 控制与 rollback：`schedx/controllers/cgroup_controller.py`
- scx 检测：`schedx/controllers/scx_controller.py`
- benchmark/report 占位：`schedx/benchmark/runner.py`、`schedx/report/report_generator.py`

第二阶段请优先完善以下内容：

1. 完善真实 benchmark 闭环：
   - 实现 nginx latency benchmark，调用 `wrk`，解析 QPS、avg latency、p95、p99。
   - 实现 redis benchmark，调用 `redis-benchmark`，解析 QPS 和延迟指标。
   - 实现 batch benchmark，调用 `sysbench cpu` 或可配置命令，记录完成时间。
   - 输出文件固定为：
     - `results/nginx_default.json`
     - `results/nginx_schedx.json`
     - `results/redis_default.json`
     - `results/redis_schedx.json`
     - `results/batch_default.json`
     - `results/batch_schedx.json`

2. 完善 cgroup-only 可演示优化：
   - `schedx optimize --target nginx --mode latency_first --apply` 能找到 nginx PID，移动到高权重 cgroup。
   - 自动识别 `stress-ng/stress`，移动到低权重 cgroup。
   - 写入前记录 rollback，失败时自动回滚。
   - 检查 cgroup v2 是否可写，不可写时输出清晰错误和修复建议。

3. 强化 workload 感知：
   - 读取 `/proc/[pid]/sched`，提取 `nr_switches`、`se.sum_exec_runtime`、`se.statistics.wait_sum` 等可用字段。
   - 读取 cgroup `cpu.stat`、`cpu.pressure`。
   - 分类结果输出 reason，例如 “comm=stress-ng and cpu_percent>50”。

4. 完善 report：
   - `schedx report` 自动读取 results JSON。
   - 生成 before/after 对比表。
   - 计算提升比例，例如 p99 latency 降低百分比、QPS 提升百分比、batch time 降低百分比。
   - 如果 matplotlib 可用，生成 `reports/figures/*.png`；不可用则只生成 Markdown 表格。

5. scx 第二阶段先做可替换适配层：
   - `ScxController.start_scheduler()` 只能启动白名单 scx 程序。
   - 不允许执行任意 shell。
   - 如果 `/sys/kernel/sched_ext` 不存在，继续 fallback 到 cgroup-only。
   - 在 `scx/README.md` 中说明后续自定义 `scx_agent` 设计。

6. 增加测试：
   - benchmark 输出解析单元测试。
   - cgroup dry-run 和 rollback 测试。
   - classifier reason 测试。
   - report 生成测试。

请直接修改代码，不要只给概念。保持所有危险操作默认 dry-run，只有显式 `--apply` 才写 cgroup 或启动调度器。实现完成后运行：
- `python -m compileall schedx`
- `python -m pytest -q` 如果 pytest 可用
- `python -m schedx.main status`
- `python -m schedx.main optimize --target nginx --mode latency_first --dry-run`
- `python -m schedx.main benchmark nginx-latency --output results`
- `python -m schedx.main report`
```
