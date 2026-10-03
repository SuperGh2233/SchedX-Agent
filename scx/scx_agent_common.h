/* SPDX-License-Identifier: GPL-2.0 */
#ifndef SCX_AGENT_COMMON_H
#define SCX_AGENT_COMMON_H

#define SCX_CLASS_UNKNOWN      0
#define SCX_CLASS_LATENCY      1
#define SCX_CLASS_BATCH        2
#define SCX_CLASS_BACKGROUND   3
#define SCX_CLASS_COUNT        4

struct schedx_policy {
	__u32 class_id;
	__u32 weight;
};

struct schedx_config {
	__u32 background_interval;
	__u32 default_interval;
};

struct schedx_cgroup_metrics {
	__u64 enqueues;
	__u64 runs;
	__u64 runtime_ns;
	__u64 wait_ns;
};

struct schedx_class_metrics {
	__u64 enqueues;
	__u64 runs;
	__u64 runtime_ns;
	__u64 wait_ns;
	__u64 max_wait_ns;
};

#endif
