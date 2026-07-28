# SchedX-Agent 最终展示视频录制台本

建议视频时长：6～8 分钟。  
录屏工具：FinalShell 录屏、OBS 或其他桌面录制工具。  
演示环境：openEuler 24.03 LTS SP4，`/root/SchedX-Agent`。

## 录制前准备

1. 将终端字体调到 18～20，窗口最大化，分辨率建议 1920×1080。
2. 不要展示 `/etc/schedx/llm.env`、API Key、SSH 私钥和密码。
3. 在开始录屏前执行：

```bash
cd /root/SchedX-Agent
source .venv/bin/activate
git pull --ff-only gitlink master
bash scripts/demo_cleanup.sh
clear
```

4. 开始录屏后执行：

```bash
cd /root/SchedX-Agent
source .venv/bin/activate
bash scripts/demo_recording_story.sh
```

脚本会在每一幕结束后等待按 Enter。讲解完当前画面，再按 Enter 继续。

## 第 1 幕：项目定位与真实环境（约 45 秒）

画面会展示 openEuler、补丁内核、cgroup v2、native sched_ext 和 LLM 状态。

旁白：

> 本项目名为 SchedX-Agent，面向 Linux 混合负载场景构建自适应资源管控闭环。当前演示运行在 openEuler 24.03 LTS SP4 上，内核已经启用 sched_ext。系统同时支持 cgroup v2 安全回退，并配置了 DeepSeek 策略模型。下面所有分类、调度、验证和回滚都在真实虚拟机中执行。

看到 `READY` 后按 Enter。

## 第 2 幕：Workload 感知与分类（约 50 秒）

脚本会启动 nginx 与 stress-ng 混合负载，并输出分类结果。

旁白：

> Agent 首先从 procfs、PSI、cgroup 和调度指标中感知工作负载。这里 nginx 被识别为延迟敏感型服务，stress-ng 及其 CPU worker 被识别为后台干扰负载，因此 overall 判定为 mixed。分类器采用受控规则，不会把 schedx、sshd、systemd 等控制进程误隔离。

看到 `latency_sensitive`、`background_noise` 和 `overall = mixed` 后按 Enter。

## 第 3 幕：LLM 提案与受约束决策（约 60 秒）

脚本会调用 `llm-plan`，显示策略来源、模式、目标、置信度和结构化参数。

旁白：

> DeepSeek 不直接生成任意 Shell 命令，而是提出结构化策略。策略必须经过参数范围、目标白名单和风险约束校验，再由专家策略路由器选择 latency guard、background isolation、balanced 或 throughput boost。即使模型不可用，系统也会自动回退到规则策略。

重点指出：

- `source = deepseek-v4`
- `mode = latency_first`
- `target = nginx`
- `parameters` 为结构化参数

讲解完后按 Enter。

## 第 4 幕：Agent 闭环执行（约 2～3 分钟）

脚本调用当前主程序：

```bash
python3 scripts/run_competition_demo.py \
  --output results/video-recording \
  --report reports/video-recording.md \
  --llm-policy \
  --compact \
  --duration 3 \
  --repeats 1 \
  --connections 8 \
  --threads 2 \
  --batch-threads 2 \
  --stress-cpu 2
```

等待期间旁白：

> Agent 正在执行完整闭环：Probe 感知系统状态，Analyze 识别 workload，LLM Policy 生成受约束提案，Policy Router 选择专家策略，随后通过 native sched_ext 与 cgroup v2 执行动作。Canary 阶段会比较执行前后的吞吐、P99 延迟和后台任务保留率，只有满足 SLO 才接受策略，否则自动回滚。

> 本次录制使用短时参数验证流程完整性，正式性能结论来自多轮重复实验，短时结果不用于宣称稳定性能提升。

脚本完成后按 Enter。

## 第 5 幕：证据链与回滚（约 90 秒）

画面会显示四组消融、sysbench 第二 workload、Canary 接受与拒绝结果。

旁白：

> 这里展示 Agent 的证据链。四组消融分别是默认调度、cgroup-only、scx-only 和 Agent 联合策略；第二个 workload 使用 sysbench 验证批处理场景。常规 Canary 满足门槛后接受策略，严格 Canary 检测到吞吐回退后拒绝策略，并恢复 CPU 权重、删除 cgroup 和 scx 策略项。

重点指出：

- `source=deepseek-v4`
- `expert=latency_guard`
- `final_status=success`
- 严格门槛下 `final_status=rolled_back`
- `rollback` 中恢复项和删除项数量

如果某个短时采样指标显示 `N/A`，说明该指标在短时窗口内不足以形成有效比较，不代表伪造或补填数据。

讲解完后按 Enter。

## 第 6 幕：清理与总结（约 45 秒）

画面应显示：

```text
stress-ng running : false
cgroup remains    : false
sched_ext state   : disabled
CLEAN: 演示环境已恢复
```

旁白：

> 演示结束后，Agent 停止干扰负载、回滚资源配置、清除 cgroup，并将 sched_ext 恢复为 disabled。由此形成感知、决策、执行、验证、接受或回滚、报告和清理的完整闭环。SchedX-Agent 的核心价值不是单次调参，而是面向 SLO 的安全自适应调度。

## 录制结束后的检查

停止录屏前再展示一次版本：

```bash
git rev-parse --short HEAD
python3 -m schedx status
```

最终提交版本应与 GitLink `master` 一致。

录制完成后，将视频命名为：

```text
项目展示.mp4
```

替换仓库根目录和 `初赛作品提交材料/` 中的旧视频，再提交：

```bash
git add 项目展示.mp4 初赛作品提交材料/项目展示.mp4
git commit -m "Update final demonstration video"
git push origin master
```

视频不得超过 100 MB。
