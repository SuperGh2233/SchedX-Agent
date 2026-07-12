# SchedX-Agent Experiment Report

## Summary

This report is generated from JSON files in `results/`.

## Before/After Comparison

| Scenario | Metric | Default | SchedX | Improvement | Status |
| --- | --- | ---: | ---: | ---: | --- |
| nginx | p99_ms | 5.110 | 5.530 | -8.22% | ok |
| redis | qps | 100657.510 | 98556.830 | -2.09% | ok |
| batch | elapsed_seconds | 30.000 | 30.000 | -0.00% | ok |

## Raw Results

### batch_default.json

```json
{
  "status": "ok",
  "command": [
    "sysbench",
    "cpu",
    "--time=30",
    "run"
  ],
  "returncode": 0,
  "metrics": {
    "elapsed_seconds": 30.0001,
    "events": 157302.0,
    "events_per_second": 5243.31
  },
  "stdout": "sysbench 1.0.20 (using system LuaJIT 2.1.ROLLING)\n\nRunning the test with following options:\nNumber of threads: 1\nInitializing random number generator from current time\n\n\nPrime numbers limit: 10000\n\nInitializing worker threads...\n\nThreads started!\n\nCPU speed:\n    events per second:  5243.31\n\nGeneral statistics:\n    total time:                          30.0001s\n    total number of events:              157302\n\nLatency (ms):\n         min:                                    0.17\n         avg:                                    0.19\n         max:                                    1.84\n         95th percentile:                        0.23\n         sum:                                29977.01\n\nThreads fairness:\n    events (avg/stddev):           157302.0000/0.00\n    execution time (avg/stddev):   29.9770/0.00\n\n",
  "stderr": "",
  "benchmark": "batch-cpu",
  "variant": "default",
  "timestamp": 1779203919
}
```

### batch_schedx.json

```json
{
  "status": "ok",
  "command": [
    "sysbench",
    "cpu",
    "--time=30",
    "run"
  ],
  "returncode": 0,
  "metrics": {
    "elapsed_seconds": 30.0002,
    "events": 157329.0,
    "events_per_second": 5244.17
  },
  "stdout": "sysbench 1.0.20 (using system LuaJIT 2.1.ROLLING)\n\nRunning the test with following options:\nNumber of threads: 1\nInitializing random number generator from current time\n\n\nPrime numbers limit: 10000\n\nInitializing worker threads...\n\nThreads started!\n\nCPU speed:\n    events per second:  5244.17\n\nGeneral statistics:\n    total time:                          30.0002s\n    total number of events:              157329\n\nLatency (ms):\n         min:                                    0.17\n         avg:                                    0.19\n         max:                                    3.23\n         95th percentile:                        0.23\n         sum:                                29975.58\n\nThreads fairness:\n    events (avg/stddev):           157329.0000/0.00\n    execution time (avg/stddev):   29.9756/0.00\n\n",
  "stderr": "",
  "benchmark": "batch-cpu",
  "variant": "schedx",
  "timestamp": 1779203954
}
```

### nginx_default.json

```json
{
  "status": "ok",
  "command": [
    "wrk",
    "-t2",
    "-c64",
    "-d10s",
    "--latency",
    "http://127.0.0.1/"
  ],
  "returncode": 0,
  "metrics": {
    "latency_avg_ms": 0.95,
    "latency_stdev_ms": 1.16,
    "latency_max_ms": 35.47,
    "req_per_sec_avg": 38440.0,
    "req_per_sec_stdev": 3910.0,
    "requests_per_sec": 76455.22,
    "qps": 76455.22,
    "transfer_sec": "273.35MB",
    "p50_ms": 0.684,
    "p50_latency_ms": 0.684,
    "p75_ms": 1.13,
    "p75_latency_ms": 1.13,
    "p90_ms": 1.89,
    "p90_latency_ms": 1.89,
    "p99_ms": 5.11,
    "p99_latency_ms": 5.11,
    "avg_latency_ms": 0.95
  },
  "stdout": "Running 10s test @ http://127.0.0.1/\n  2 threads and 64 connections\n  Thread Stats   Avg      Stdev     Max   +/- Stdev\n    Latency     0.95ms    1.16ms  35.47ms   91.88%\n    Req/Sec    38.44k     3.91k   58.51k    77.50%\n  Latency Distribution\n     50%  684.00us\n     75%    1.13ms\n     90%    1.89ms\n     99%    5.11ms\n  765313 requests in 10.01s, 2.67GB read\nRequests/sec:  76455.22\nTransfer/sec:    273.35MB\n",
  "stderr": "",
  "benchmark": "nginx-latency",
  "variant": "default",
  "timestamp": 1779203882
}
```

### nginx_latency.json

```json
{
  "benchmark": "nginx-latency",
  "timestamp": 1779006433,
  "status": "plan_only",
  "message": "Install wrk/redis-benchmark/sysbench on openEuler to collect real metrics.",
  "metrics": {}
}
```

### nginx_schedx.json

```json
{
  "status": "ok",
  "command": [
    "wrk",
    "-t2",
    "-c64",
    "-d10s",
    "--latency",
    "http://127.0.0.1/"
  ],
  "returncode": 0,
  "metrics": {
    "latency_avg_ms": 0.96,
    "latency_stdev_ms": 1.68,
    "latency_max_ms": 56.87,
    "req_per_sec_avg": 38280.0,
    "req_per_sec_stdev": 3150.0,
    "requests_per_sec": 76098.86,
    "qps": 76098.86,
    "transfer_sec": "272.08MB",
    "p50_ms": 0.7,
    "p50_latency_ms": 0.7,
    "p75_ms": 1.04,
    "p75_latency_ms": 1.04,
    "p90_ms": 1.62,
    "p90_latency_ms": 1.62,
    "p99_ms": 5.53,
    "p99_latency_ms": 5.53,
    "avg_latency_ms": 0.96
  },
  "stdout": "Running 10s test @ http://127.0.0.1/\n  2 threads and 64 connections\n  Thread Stats   Avg      Stdev     Max   +/- Stdev\n    Latency     0.96ms    1.68ms  56.87ms   96.08%\n    Req/Sec    38.28k     3.15k   53.94k    76.50%\n  Latency Distribution\n     50%  700.00us\n     75%    1.04ms\n     90%    1.62ms\n     99%    5.53ms\n  762093 requests in 10.01s, 2.66GB read\nRequests/sec:  76098.86\nTransfer/sec:    272.08MB\n",
  "stderr": "",
  "benchmark": "nginx-latency",
  "variant": "schedx",
  "timestamp": 1779203896
}
```

### redis_default.json

```json
{
  "status": "ok",
  "command": [
    "redis-benchmark",
    "-q",
    "-n",
    "100000",
    "-c",
    "64",
    "-t",
    "get,set"
  ],
  "returncode": 0,
  "metrics": {
    "p50_latency_ms": 0.311,
    "qps": 100657.51
  },
  "stdout": " \nSET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)\n                                                          \nSET: rps=105660.0 (overall: 105239.0) avg_msec=0.326 (overall: 0.326)\n                                                                      \nSET: rps=108051.8 (overall: 106645.4) avg_msec=0.315 (overall: 0.320)\n                                                                      \nSET: rps=99256.0 (overall: 104188.8) avg_msec=0.369 (overall: 0.336)\n                                                                     \nSET: 102986.61 requests per second, p50=0.303 msec\n \nGET: rps=10408.0 (overall: 89724.1) avg_msec=0.427 (overall: 0.427)\n                                                                    \nGET: rps=101812.8 (overall: 100560.7) avg_msec=0.345 (overall: 0.353)\n                                                                      \nGET: rps=87676.0 (overall: 94483.0) avg_msec=0.451 (overall: 0.396)\n                                                                    \nGET: rps=95156.0 (overall: 94698.7) avg_msec=0.394 (overall: 0.395)\n                                                                    \nGET: 98328.42 requests per second, p50=0.311 msec\n\n",
  "stderr": "",
  "benchmark": "redis-latency",
  "variant": "default",
  "timestamp": 1779203908
}
```

### redis_schedx.json

```json
{
  "status": "ok",
  "command": [
    "redis-benchmark",
    "-q",
    "-n",
    "100000",
    "-c",
    "64",
    "-t",
    "get,set"
  ],
  "returncode": 0,
  "metrics": {
    "p50_latency_ms": 0.311,
    "qps": 98556.83
  },
  "stdout": " \nSET: rps=0.0 (overall: -nan) avg_msec=-nan (overall: -nan)\n                                                           \nSET: rps=104744.0 (overall: 104744.0) avg_msec=0.326 (overall: 0.326)\n                                                                      \nSET: rps=93402.4 (overall: 99061.9) avg_msec=0.403 (overall: 0.362)\n                                                                    \nSET: rps=85844.0 (overall: 94661.8) avg_msec=0.444 (overall: 0.387)\n                                                                    \nSET: rps=98426.3 (overall: 95604.8) avg_msec=0.379 (overall: 0.385)\n                                                                    \nSET: 95693.78 requests per second, p50=0.311 msec\n \nGET: rps=82740.0 (overall: 101397.1) avg_msec=0.333 (overall: 0.333)\n                                                                     \nGET: rps=93796.0 (overall: 97211.5) avg_msec=0.393 (overall: 0.365)\n                                                                    \nGET: rps=104784.9 (overall: 99907.8) avg_msec=0.325 (overall: 0.350)\n                                                                     \nGET: rps=105304.0 (overall: 101320.4) avg_msec=0.319 (overall: 0.342)\n                                                                      \nGET: 101419.88 requests per second, p50=0.311 msec\n\n",
  "stderr": "",
  "benchmark": "redis-latency",
  "variant": "schedx",
  "timestamp": 1779203915
}
```
