# DeepSeek Policy Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 5s
- Repeats: 1
- LLM policy source: `deepseek-v4`
- LLM decision: `{"mode": "latency_first", "target": "nginx", "parameters": {"cpu_weight": 10000, "cpu_weight_bg": 100, "cpu_max_bg": "50000 100000"}, "reason": "DeepSeek V4 proposal: rule baseline for nginx under CPU interference", "confidence": 0.8}`
- LLM classified latency tasks: 5
- LLM classified background tasks: 5
- Rule RPS gain: 107.32%
- Rule P99 reduction: 57.63%
- Rule background retention: 10.77%
- LLM RPS gain: 109.98%
- LLM P99 reduction: 53.29%
- LLM background retention: 11.28%