# DeepSeek Policy Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 5s
- Repeats: 1
- LLM policy source: `deepseek-v4`
- LLM decision: `{"mode": "latency_first", "target": "nginx", "parameters": {"cpu_weight": 10000, "cpu_weight_bg": 100, "cpu_max_bg": "50000 100000"}, "reason": "DeepSeek V4 proposal: Prioritize nginx for latency-sensitive workload with no background noise detected.", "confidence": 0.8}`
- Rule RPS gain: 98.63%
- Rule P99 reduction: 46.93%
- Rule background retention: 11.86%
- LLM RPS gain: 98.47%
- LLM P99 reduction: 43.52%
- LLM background retention: 11.86%