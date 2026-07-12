# Multi-Agent LLM Scheduling Experiment

This experiment runs concurrent Agent tool calls through the persistent
sched_ext daemon and captures a live DeepSeek policy plan while those
tools are active.

- Agents: 6
- Duration: 8s
- Active policies observed: 6
- Successful tools: 6/6
- Native sched_ext tools: 6/6
- Daemon-mode tools: 6/6
- Policies remaining after completion: 0
- Daemon running after experiment: True
- LLM source: `deepseek-v4`
- LLM mode: `latency_first`
- LLM target: `redis-server`
- Verification passed: True