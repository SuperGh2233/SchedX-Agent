# DeepSeek Policy Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 10s
- Repeats: 3
- LLM policy source: `deepseek-v4`
- LLM decision: `{"mode": "latency_first", "target": "nginx", "parameters": {"cpu_weight": 10000, "cpu_weight_bg": 100, "cpu_max_bg": "50000 100000"}, "reason": "DeepSeek V4 proposal: nginx is latency-sensitive, stress-ng are background noise. Prioritize nginx with high weight and limit background CPU.", "confidence": 0.8}`
- LLM classified latency tasks: 5
- LLM classified background tasks: 5
- Rule RPS gain: 84.03%
- Rule P99 reduction: 55.21%
- Rule background retention: 31.31%
- LLM RPS gain: 84.96%
- LLM P99 reduction: 55.16%
- LLM background retention: 31.20%