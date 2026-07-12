# Experiment Plan

## Nginx Latency Protection

1. Start nginx.
2. Measure baseline with `wrk`.
3. Start `stress-ng --cpu 0`.
4. Run `schedx optimize --target nginx --mode latency_first --apply`.
5. Measure QPS, average latency, p95, and p99 again.

## Redis Latency Protection

1. Start redis-server.
2. Measure with `redis-benchmark`.
3. Start CPU interference.
4. Run `schedx optimize --target redis --mode latency_first --apply`.
5. Compare throughput and tail latency.

## Batch Throughput

1. Run `sysbench cpu` or `make -j`.
2. Compare default scheduler, static cgroup weights, and SchedX adaptive policy.

