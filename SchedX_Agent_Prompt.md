# SchedX-Agent 比赛项目生成 Prompt

> 适用场景：第三届中国研究生操作系统开源创新大赛  
> 赛题：系统创新 - 面向 Linux 的自适应资源管控 Agent  
> 团队：SchedX Lab  
> 用法：将本 Prompt 发送给 GPT-5.5 / Codex / Cursor，用于生成项目设计、MVP 代码、实验脚本和技术报告框架。

---

## 角色设定

你是一名资深 Linux 操作系统内核与系统工程开发专家，熟悉 openEuler、Linux 调度器、procfs、cgroup v2、eBPF、sched_ext/scx、性能测试与开源项目工程化实践。现在请你作为我的技术架构师和代码生成助手，帮助我完成“第三届中国研究生操作系统开源创新大赛”的系统创新赛道项目。

---

## 参赛项目信息

团队名称：SchedX Lab  
项目方向：系统创新 - 面向 Linux 的自适应资源管控 Agent  
目标平台：openEuler 24.03-LTS-SP3  
项目名称建议：AutoSCX-Agent 或 SchedX-Agent

---

## 赛题要求

设计并实现一个基于用户态调度的资源管控 Agent 框架，要求包括：

1. 实现标准化的工具和 Skills 能力接口，支持 Agent 快速构建和扩展；
2. Agent 能够感知 workloads；
3. Agent 能够基于 sched_ext 调整 CPU 调度策略；
4. 集成 scx 调度器，实现对 workload 性能的优化；
5. 支持扩展 eBPF 作为 hook，实现 network policy agent、security policy agent、resource control agent 等扩展能力；
6. 提供完整的性能对比测试数据和可复现的实验环境；
7. 最终代码需要能在 openEuler 24.03-LTS-SP3 上正常编译、运行和测试。

评分细则：

- 创新性 30 分：Agent 架构设计的新颖性，调度策略创新程度；
- 功能完整性 25 分：workload 感知能力、调度策略调整的准确性和灵活性；
- 性能提升 25 分：相比默认调度器的性能提升幅度；
- 代码质量 10 分：代码结构、注释和可维护性；
- 演示效果 10 分：现场演示流畅度和说服力。

---

## 项目总体目标

请帮我设计并逐步实现一个名为 **SchedX-Agent** 的系统。它不是普通聊天机器人，而是一个面向 Linux 混合负载场景的自适应资源管控 Agent。

系统应形成如下闭环：

```text
workload 感知 → 负载分类 → 策略决策 → sched_ext/scx 调度执行 → cgroup 资源控制 → 性能验证 → 自动生成报告 → 异常回滚
```

重点场景：

### 1. 在线服务保护场景

- nginx 或 redis 作为 latency-sensitive workload；
- stress-ng 或 CPU 密集型程序作为 background workload；
- Agent 自动识别干扰进程，并通过 scx 调度策略或 cgroup 限制降低在线服务 p95/p99 延迟。

### 2. 批处理吞吐优化场景

- make -j、sysbench 或 CPU 密集型任务作为 batch workload；
- Agent 识别批处理任务，选择吞吐优先策略；
- 对比默认调度器与 Agent 优化后的任务完成时间。

### 3. 混合负载场景

- 在线服务 + 后台 CPU 任务同时运行；
- Agent 根据 CPU pressure、进程 CPU 占用、服务延迟等指标动态调整策略。

---

## 技术路线要求

项目采用分层架构，建议包括以下模块。

---

## 1. workload 感知层

需要从以下来源采集数据：

- `/proc/stat`
- `/proc/[pid]/stat`
- `/proc/[pid]/sched`
- `/proc/pressure/cpu`
- `/proc/pressure/memory`
- `/sys/fs/cgroup`
- 可选：eBPF tracepoint，例如 `sched_switch`、`sched_wakeup`

需要采集的指标包括：

- 进程 PID、命令名、CPU 占用、内存占用；
- 上下文切换次数；
- 运行队列等待时间；
- CPU pressure；
- memory pressure；
- cgroup CPU 使用情况；
- 服务性能指标，例如 wrk 输出的 QPS、平均延迟、p95/p99 延迟。

---

## 2. workload 分类层

实现规则化分类，不要完全依赖大模型。

至少支持以下类型：

- `latency_sensitive`：nginx、redis、在线推理服务等；
- `batch_compute`：make、gcc、sysbench、矩阵计算等；
- `background_noise`：stress-ng、后台高 CPU 程序等；
- `mixed`：多个 workload 同时存在。

请设计一个可扩展的分类器，例如 `WorkloadClassifier`。

---

## 3. Agent Skills 接口层

实现标准化 Skill 接口，体现赛题要求中的“标准化工具和 Skills 能力接口”。

建议包括：

- `ProbeSkill`：采集系统状态；
- `AnalyzeSkill`：分析 workload 类型；
- `PolicySkill`：生成资源管控策略；
- `ActSkill`：执行调度或 cgroup 动作；
- `VerifySkill`：执行性能验证；
- `RollbackSkill`：失败时回滚配置；
- `ReportSkill`：生成实验报告。

所有 Skill 应该有统一接口，例如：

```python
class Skill:
    name: str
    description: str

    def run(self, context: AgentContext) -> SkillResult:
        pass
```

---

## 4. 策略决策层

Agent 不应直接执行大模型生成的任意 shell 命令。  
请实现安全的结构化 Action Schema，例如：

```json
{
  "action": "set_cgroup_cpu_weight",
  "target": "stress-ng",
  "target_type": "process",
  "value": 100,
  "reason": "background workload is interfering with latency-sensitive service",
  "risk_level": "medium",
  "rollback_enabled": true
}
```

策略类型至少包括：

- `latency_first`
- `throughput_first`
- `balanced`
- `isolate_background`
- `rollback_to_default`

---

## 5. sched_ext/scx 接入层

需要设计 scx 适配层，即使当前机器暂时没有 sched_ext，也要有 feature detection 和 fallback。

要求：

- 检查 `/sys/kernel/sched_ext` 是否存在；
- 检查 `/sys/kernel/sched_ext/state`；
- 检查当前 scx 调度器状态；
- 支持加载、停止、切换 scx 调度器；
- 如果 sched_ext 不可用，系统应降级为 cgroup-only 模式，同时给出清晰提示。

请设计 `ScxController`，例如：

```python
class ScxController:
    def is_available(self) -> bool:
        pass

    def current_scheduler(self) -> str:
        pass

    def start_scheduler(self, scheduler_name: str, args: list[str]) -> bool:
        pass

    def stop_scheduler(self) -> bool:
        pass
```

如果可以，请提供一个最小可运行的自定义 `scx_agent` 调度器设计方案。  
它可以通过 BPF map 或用户态配置接收 workload 类型和权重。  
如果完整 scx 调度器实现复杂，请先提供接口、README 和可替换的适配层，再逐步实现。

---

## 6. cgroup 资源控制层

实现 cgroup v2 控制器，作为调度策略的补充手段。

至少支持：

- 创建 cgroup；
- 将 PID 移入 cgroup；
- 设置 `cpu.weight`；
- 设置 `cpu.max`；
- 读取 `cpu.pressure`；
- 回滚原始配置。

请设计 `CgroupController`，例如：

```python
class CgroupController:
    def create_group(self, name: str) -> str:
        pass

    def add_pid(self, group: str, pid: int) -> None:
        pass

    def set_cpu_weight(self, group: str, weight: int) -> None:
        pass

    def set_cpu_max(self, group: str, quota: str) -> None:
        pass

    def read_cpu_pressure(self, group: str) -> dict:
        pass
```

---

## 7. eBPF 扩展层

项目主线是 CPU 调度与资源管控，但需要预留 eBPF hook 扩展能力。

请设计 eBPF 插件接口，例如：

- `SchedulerTraceHook`：监听 `sched_switch`；
- `NetworkPolicyHook`：预留网络策略接口；
- `SecurityPolicyHook`：预留安全策略接口；
- `ResourceControlHook`：预留资源控制接口。

第一版可以先实现接口和 mock 数据，第二版再接入真实 eBPF 程序。

---

## 8. CLI 命令行工具

请实现一个清晰的命令行工具，例如 `schedx`。

至少支持以下命令：

```bash
schedx status
schedx probe
schedx classify
schedx optimize --target nginx --mode latency_first
schedx optimize --target redis --mode latency_first
schedx control --pid 1234 --cpu-weight 100
schedx benchmark nginx-latency
schedx benchmark redis-latency
schedx rollback
schedx report
```

---

## 9. Benchmark 和可复现实验

请提供一键实验脚本。

至少包括：

### nginx 延迟保护实验

- 启动 nginx；
- 使用 wrk 进行压测；
- 启动 stress-ng 制造 CPU 干扰；
- 对比默认调度器与 SchedX-Agent 优化后的 QPS、平均延迟、p95/p99 延迟。

### redis 延迟保护实验

- 启动 redis；
- 使用 redis-benchmark 测试；
- 启动 stress-ng 制造 CPU 干扰；
- 对比优化前后 QPS 和延迟。

### batch 吞吐实验

- 使用 sysbench cpu 或 make -j；
- 对比默认调度器、固定策略、SchedX-Agent 自适应策略下的完成时间。

实验结果需要保存为：

```text
results/
  nginx_default.json
  nginx_schedx.json
  redis_default.json
  redis_schedx.json
  batch_default.json
  batch_schedx.json
```

并自动生成：

```text
reports/
  report.md
  figures/
    nginx_latency.png
    redis_qps.png
    batch_time.png
```

---

## 10. 项目结构要求

请生成一个工程化项目结构，建议如下：

```text
SchedX-Agent/
  README.md
  docs/
    design.md
    experiment.md
    openEuler_setup.md
  schedx/
    __init__.py
    main.py
    agent/
      context.py
      skill.py
      planner.py
      executor.py
    skills/
      probe_skill.py
      analyze_skill.py
      policy_skill.py
      act_skill.py
      verify_skill.py
      rollback_skill.py
      report_skill.py
    probes/
      procfs_probe.py
      pressure_probe.py
      cgroup_probe.py
      ebpf_probe.py
    controllers/
      scx_controller.py
      cgroup_controller.py
      process_controller.py
    policies/
      latency_first.py
      throughput_first.py
      balanced.py
      isolate_background.py
    benchmark/
      nginx_bench.py
      redis_bench.py
      sysbench_bench.py
    report/
      report_generator.py
  scx/
    README.md
    scx_agent.bpf.c
    scx_agent_user.c
  scripts/
    install_deps_openeuler.sh
    check_env.sh
    run_nginx_experiment.sh
    run_redis_experiment.sh
    run_batch_experiment.sh
  tests/
    test_procfs_probe.py
    test_cgroup_controller.py
    test_policy.py
  results/
  reports/
  pyproject.toml
```

如果你认为 Python 不适合某些底层模块，可以提出更合理的语言组合，例如 Python + C/eBPF，或者 Go + C/eBPF。  
但请保证项目容易实现、容易演示、容易复现。

---

## 开发要求

请遵守以下要求：

1. 不要只给概念，要给可以落地的代码；
2. 每个模块都要有清晰职责；
3. 所有危险操作都要有 dry-run 模式；
4. 所有资源管控操作都要支持 rollback；
5. 命令执行必须通过白名单，不允许 Agent 任意执行 shell；
6. 代码要有注释；
7. README 要写清楚如何在 openEuler 24.03-LTS-SP3 上安装、运行、测试；
8. 如果 sched_ext/scx 在当前环境不可用，请提供检测逻辑和 fallback 策略；
9. 实验脚本要尽可能自动化；
10. 输出时请优先生成 MVP，再逐步扩展，不要一开始生成不可运行的大工程。

---

## 第一阶段任务

请先完成以下内容：

1. 给出完整系统架构设计；
2. 给出项目目录结构；
3. 给出核心模块职责说明；
4. 生成第一版可运行 MVP 代码，包括：
   - CLI 入口；
   - procfs workload 采集；
   - workload 分类；
   - cgroup v2 控制；
   - scx 可用性检测；
   - optimize 命令；
   - rollback 命令；
   - 简单 benchmark 框架；
5. 给出如何运行的命令；
6. 给出下一阶段如何接入真实 scx 调度器和 eBPF hook 的计划。

---

## 输出格式

请按以下格式输出：

```markdown
## 1. 项目总体设计

## 2. 技术架构图，使用 Mermaid 表示

## 3. 项目目录结构

## 4. 第一版 MVP 功能列表

## 5. 核心代码

请按文件路径分别给出代码。

## 6. 运行方法

## 7. openEuler 环境依赖安装

## 8. 实验与性能测试方法

## 9. 后续迭代计划

## 10. 风险点与解决方案
```

请注意：这是一个比赛项目，重点不是做普通 AI 聊天助手，而是做一个有 Linux 系统深度、能运行、能测试、有性能数据、有演示效果的自适应资源管控 Agent。

---

# 附加追问 Prompt

第一轮生成内容后，可以继续发送下面这段，让模型优先实现 MVP，而不是生成空架子。

```markdown
请基于上面的项目设计，先只实现第一阶段 MVP，要求代码可以直接复制到本地运行。不要生成过多空文件，优先保证 schedx status、schedx probe、schedx classify、schedx optimize、schedx rollback 这几个命令可运行。
```
