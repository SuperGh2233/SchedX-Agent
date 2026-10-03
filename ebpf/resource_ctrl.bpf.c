/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Resource-policy telemetry for cgroup v2 workloads.
 *
 * eBPF observes policy hits and scheduler activity. Hard CPU, memory and I/O
 * limits remain the responsibility of cgroup v2; a tracepoint program cannot
 * safely replace those controllers.
 */

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>

struct resource_policy {
    __u32 class_id;
    __u32 reserved;
    __u64 memory_limit;
    __u32 cpu_weight;
    __u32 io_weight;
};

struct resource_usage {
    __u64 sched_switches;
    __u64 policy_hits;
    __u64 process_exits;
};

struct global_resource_stats {
    __u64 sched_switches;
    __u64 policy_hits;
    __u64 process_exits;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, __u64);
    __type(value, struct resource_policy);
} res_policy_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __uint(max_entries, 1024);
    __type(key, __u64);
    __type(value, struct resource_usage);
} res_usage_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct global_resource_stats);
} res_global_map SEC(".maps");

static __always_inline struct resource_usage *usage_for(__u64 cgroup_id)
{
    struct resource_usage *usage = bpf_map_lookup_elem(&res_usage_map, &cgroup_id);
    if (usage)
        return usage;

    struct resource_usage initial = {};
    bpf_map_update_elem(&res_usage_map, &cgroup_id, &initial, BPF_NOEXIST);
    return bpf_map_lookup_elem(&res_usage_map, &cgroup_id);
}

SEC("tp/sched/sched_switch")
int trace_resource_switch(void *ctx)
{
    __u64 cgroup_id = bpf_get_current_cgroup_id();
    struct resource_usage *usage = usage_for(cgroup_id);
    struct resource_policy *policy = bpf_map_lookup_elem(&res_policy_map, &cgroup_id);
    __u32 key = 0;
    struct global_resource_stats *global = bpf_map_lookup_elem(&res_global_map, &key);

    if (usage)
        usage->sched_switches++;
    if (global)
        global->sched_switches++;
    if (policy) {
        if (usage)
            usage->policy_hits++;
        if (global)
            global->policy_hits++;
    }
    return 0;
}

SEC("tp/sched/sched_process_exit")
int trace_resource_exit(void *ctx)
{
    __u64 cgroup_id = bpf_get_current_cgroup_id();
    struct resource_usage *usage = usage_for(cgroup_id);
    __u32 key = 0;
    struct global_resource_stats *global = bpf_map_lookup_elem(&res_global_map, &key);

    if (usage)
        usage->process_exits++;
    if (global)
        global->process_exits++;
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
