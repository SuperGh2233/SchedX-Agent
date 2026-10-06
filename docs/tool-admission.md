# 工具准入与自适应并发

本轮在既有工具启动握手、资源合同和清理基础上，增加工具执行前的共享队列。它只管理经过同一个准入目录提交的工具，不控制其他入口的任务，也不实现内核 CPU 配额或线程级组公平性。

## 使用

功能默认关闭，显式启用后才加入共享队列。同一机器、同一权限域的调用方须使用相同目录及参数；在仍有任务时遇到不同配置会拒绝请求，不能悄悄改动其他调用方的额度。

```bash
sudo schedx tool-run --agent-id agent-a \
  --admission adaptive --admission-state /run/schedx/tool-admission \
  --admission-limit 4 --admission-min 1 --admission-max 8 \
  --interactive-reserve 1 --timeout 60 -- make -j2

sudo schedx tool-run --agent-id agent-b \
  --admission adaptive --admission-state /run/schedx/tool-admission \
  --admission-limit 4 --admission-min 1 --admission-max 8 \
  --interactive-reserve 1 --timeout 60 -- git status
```

`--admission fixed` 提供相同排队规则和固定额度，便于对照。`--admission off` 沿用已有直接执行路径。Python 调用方可以将同一目录和配置的 `ToolAdmission` 传入 `ToolCallRunner(admission=...)`。

## 已实现的合同

- **跨进程共享**：独立 CLI 和 Python 调用方通过带锁状态协调；不是每个进程各自持有一个信号量。队列默认最多 128 个等待请求。
- **优先级与等待老化**：交互优先，然后测试、编译/安装、后台；等待达到两秒的可执行请求按提交顺序获得机会。同优先级考虑 Agent 最近服务顺序，避免一个 Agent 大量提交占据队头。它不构成运行时间份额或硬实时保证。
- **交互名额**：默认保留一个并发位置，非交互任务不能全部借用。至少允许一个非交互任务使用额度；额度为一时没有独立保留位置。不抢占已经运行的任务，这个取舍需要与总吞吐一同测量。
- **总时限**：`--timeout` 从本次调用提交时计算，包括排队与启动；排队耗尽预算返回 124，实际命令没有启动。启动期间超时也保持工作命令的执行门关闭。执行超时仍清理整个工具进程树。
- **租约与恢复**：租约描述符随工具继承；调用方异常退出时，仍运行的工具或其占用的原 cgroup 保留名额。只有租约失效且该工具没有存活负载时才回收；准入恢复不伪装成工具旧策略的完整恢复。
- **可追溯结果**：保存 `queue_wait_seconds`、`startup_seconds`、`execution_seconds`、`duration_seconds`、`command_started`、`timeout_phase`、准入额度与反馈原因。总耗时包括清理，不能只比较进入执行后的时延。

## 首版自适应规则

使用 Linux CPU、内存和 I/O PSI 的累计阻塞时间，按约一秒窗口计算阻塞比例。连续两个高压力窗口降低额度，连续三个低压力窗口且存在排队需求时尝试增加一个名额；调整有三秒冷却，并受最小/最大额度限制。已有任务不被中途取消，降低额度仅影响后续启动。

当前试验阈值为 CPU 20%、内存/I/O 10% 的高压力门槛，以及 CPU 5%、内存/I/O 1% 的低压力门槛。它们是工程参数，已在下述四 vCPU 场景对测与长测中验证；不构成跨环境最优参数。缺失、重置或无效的压力数据不能支持扩容。近期真实执行超时会阻止立即扩容，历史超时在 30 秒后不再永久阻塞恢复。

首版记录工具完成耗时，但没有将不同命令的原始耗时混合成一个“最小 RTT”，也未声称实现完整 Envoy 控制器或学习路由。参考 [Envoy 自适应并发设计](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/adaptive_concurrency_filter) 与 [Linux PSI 文档](https://docs.kernel.org/accounting/psi.html)。

## 验证与边界

本地测试覆盖真实独立进程共享额度、继承租约、优先级、老化、保留名额、队列容量、取消、损坏状态、锁超时、CPU 合同拒绝后回收，以及排队时间消耗执行预算。文件系统替身测试真实执行子程序与进程树清理，但不代表 Linux cgroup、PSI 或调度性能已重新验收。

Linux 对照入口：

```bash
# 首轮校准；重复不足五轮时只会标记 calibration_only。
sudo python3 scripts/verify_tool_admission.py \
  --output /tmp/schedx-admission-calibration --repeats 1

# 五轮轮换 off/fixed/adaptive，保留全部结果和失效样本。
sudo python3 scripts/verify_tool_admission.py \
  --output /tmp/schedx-admission-paired --repeats 5
```

该脚本要求默认调度器已启用、父 cgroup 控制器已配置，不停止其他调度器、不改动全局控制配置。它建立独立临时资源组，控制器/测量留在一颗 CPU，工具使用其余 CPU，比较提交到完成的时延、成功完成吞吐和后台 CPU 进度。每批默认只有六个前台任务，最近秩 P99 接近该批最大值，应作为批次尾部指标；要做稳定分位数声明还需增加样本。

五对有效样本和后台进度至少保留 25% 是基础门槛，主要指标置信区间显示稳定超过 5% 的退化会阻止通过。门禁通过不等于显著性能增益，增益声明还需相应区间支持。对照对象是当前 `ToolCallRunner` 的三种入口模式，不是未经适配的完整初赛 Agent。

实机阶段已完成长时间持续提交、负载突变、调用方崩溃/遗留后代，以及 30 分钟和两小时状态、清理与等待验收。最新实施及验收状态见 [本轮报告](../reports/optimization-admission-20261006/report.md)。

## 实机最终验收 — 2026-10-06

冻结候选 `5ac43a8` 在 Linux 通过 452 项测试、五轮对照、跨独立进程异常恢复、30 分钟和两小时验收。长测暴露的资源组父目录创建/删除竞争已通过跨进程拓扑锁修正，重新执行全部新路径验收；旧失败完整保留。功能仍默认关闭。

调试增加了状态版本缓存、变化时写回、Linux pidfd 完成通知与有界拓扑锁。运行状态放在生产默认 `/run`，结束后复制完整状态到持久证据目录；自定义磁盘目录的同步开销应独立测量。

实测配置为初始额度 **5**、范围 **1–8**、交互保留 **2**，CLI 原默认值仍是 4/8/1。五轮相对关闭准入，固定/自适应交互批次尾部总耗时下降 65.73%/63.54%，成功工具吞吐下降 2.09%/4.48%。每批六个交互样本，属于本场景批次尾部指标；它不是服务 P99 或初赛完整系统收益。自适应优于固定尚未证明，本场景固定模式的吞吐取舍更小。

两小时完成 55660 次工具调用与 60 次异常恢复，无失败；交互最长排队 134.13 ms，编译 5.27 秒。最终专属资源组、策略和挂钩清理通过，原目录/安装程序未覆盖，父控制器恢复原集合。最长等待为观测值，不是硬实时保证。

当前 SP4 内核支持 PSI 但默认关闭，使用真实压力反馈需要启用该内核能力。本次 `psi=1` 仅用于测试启动，原启动配置已恢复；缺失压力统计时保持有界额度并报告缺失状态，不能声称压力反馈生效。完整配置、置信区间、失败历史与原始证据见 [最终报告](../reports/optimization-admission-20261006/report.md)。
