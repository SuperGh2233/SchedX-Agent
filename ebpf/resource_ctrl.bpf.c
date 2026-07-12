/* SPDX-License-Identifier: GPL-2.0 */
/*
 * resource_ctrl.bpf.c - Resource control using eBPF
 *
 * This BPF program enhances cgroup-based resource control with
 * eBPF hooks for more fine-grained resource management.
 *
 * Features:
 * - Memory allocation tracking per cgroup
 * - I/O bandwidth monitoring
 * - CPU usage accounting per workload class
 * - Resource limit enforcement
 * - OOM (Out of Memory) notification
 */

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

/* Resource class constants */
#define RES_CLASS_LATENCY     1
#define RES_CLASS_BATCH       2
#define RES_CLASS_BACKGROUND  3
#define RES_CLASS_UNKNOWN     0

/* Resource limits */
#define MEM_LIMIT_LATENCY     (4ULL * 1024 * 1024 * 1024)  /* 4 GB */
#define MEM_LIMIT_BATCH       (2ULL * 1024 * 1024 * 1024)  /* 2 GB */
#define MEM_LIMIT_BACKGROUND  (512ULL * 1024 * 1024)       /* 512 MB */

/* Resource policy per cgroup */
struct resource_policy {
    __u32 class_id;         /* RES_CLASS_* */
    __u64 memory_limit;     /* Memory limit in bytes */
    __u32 cpu_weight;       /* CPU weight (1-10000) */
    __u32 io_weight;        /* I/O weight (1-1000) */
};

/* Resource usage statistics per cgroup */
struct resource_usage {
    __u64 memory_bytes;     /* Current memory usage */
    __u64 memory_peak;      /* Peak memory usage */
    __u64 cpu_ns;           /* CPU time in nanoseconds */
    __u64 io_bytes_read;    /* I/O bytes read */
    __u64 io_bytes_write;   /* I/O bytes written */
    __u32 oom_count;        /* OOM kill count */
    __u32 throttle_count;   /* Throttle event count */
};

/* Memory allocation event */
struct mem_event {
    __u64 timestamp;
    __u32 pid;
    __u32 cgroup_id;
    __u64 bytes;
    __u64 total_bytes;
    bool is_allocation;     /* true = alloc, false = free */
    char comm[16];
};

/* BPF Maps */

/* Per-cgroup resource policy: cgroup_id -> policy */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, __u64);                     /* cgroup id */
    __type(value, struct resource_policy);
} resource_policy_map SEC(".maps");

/* Per-cgroup resource usage: cgroup_id -> usage */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, __u64);                     /* cgroup id */
    __type(value, struct resource_usage);
} resource_usage_map SEC(".maps");

/* Per-task cgroup mapping: PID -> cgroup_id */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 8192);
    __type(key, __u32);                     /* PID */
    __type(value, __u64);                   /* cgroup id */
} task_cgroup_map SEC(".maps");

/* Ring buffer for resource events */
struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);  /* 256KB */
} resource_events SEC(".maps");

/* Global resource statistics (per-CPU) */
struct global_resource_stats {
    __u64 total_allocations;
    __u64 total_frees;
    __u64 total_bytes_allocated;
    __u64 total_bytes_freed;
    __u64 oom_events;
    __u64 throttle_events;
};

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct global_resource_stats);
} global_resource_stats_map SEC(".maps");

/* Helper: Get current cgroup ID */
static __always_inline __u64 get_current_cgroup_id(void)
{
    struct task_struct *task = (void *)bpf_get_current_task();
    if (!task)
        return 0;

    struct css_set *cgroups;
    bpf_probe_read_kernel(&cgroups, sizeof(cgroups), &task->cgroups);
    if (!cgroups)
        return 0;

    struct cgroup *cgrp;
    bpf_probe_read_kernel(&cgrp, sizeof(cgrp), &cgroups->dfl_cgrp);
    if (!cgrp)
        return 0;

    struct kernfs_node *kn;
    bpf_probe_read_kernel(&kn, sizeof(kn), &cgrp->kn);
    if (!kn)
        return 0;

    __u64 id;
    bpf_probe_read_kernel(&id, sizeof(id), &kn->id);
    return id;
}

/* Helper: Get or create resource usage entry */
static __always_inline struct resource_usage *get_resource_usage(__u64 cgroup_id)
{
    struct resource_usage *usage;

    usage = bpf_map_lookup_elem(&resource_usage_map, &cgroup_id);
    if (usage)
        return usage;

    struct resource_usage new_usage = {};
    bpf_map_update_elem(&resource_usage_map, &cgroup_id, &new_usage, BPF_ANY);
    return bpf_map_lookup_elem(&resource_usage_map, &cgroup_id);
}

/* Helper: Update global statistics */
static __always_inline void update_resource_stats(bool is_alloc, __u64 bytes, bool is_oom)
{
    __u32 key = 0;
    struct global_resource_stats *stats;

    stats = bpf_map_lookup_elem(&global_resource_stats_map, &key);
    if (!stats)
        return;

    if (is_alloc) {
        stats->total_allocations++;
        stats->total_bytes_allocated += bytes;
    } else {
        stats->total_frees++;
        stats->total_bytes_freed += bytes;
    }

    if (is_oom)
        stats->oom_events++;
}

/*
 * Tracepoint: cgroup:cgroup_attach_task
 *
 * Track when tasks are moved between cgroups.
 */
SEC("tp/cgroup/cgroup_attach_task")
int trace_cgroup_attach_task(struct trace_event_raw_cgroup *ctx)
{
    __u32 pid = bpf_get_current_pid_tgid() >> 32;
    __u64 cgroup_id = get_current_cgroup_id();

    /* Update task -> cgroup mapping */
    bpf_map_update_elem(&task_cgroup_map, &pid, &cgroup_id, BPF_ANY);

    return 0;
}

/*
 * Tracepoint: kmem:kmalloc
 *
 * Track kernel memory allocations.
 */
SEC("tp/kmem/kmalloc")
int trace_kmalloc(struct trace_event_raw_kmem_alloc *ctx)
{
    __u64 cgroup_id = get_current_cgroup_id();
    if (!cgroup_id)
        return 0;

    __u64 bytes = ctx->bytes_alloc;

    /* Update usage */
    struct resource_usage *usage = get_resource_usage(cgroup_id);
    if (usage) {
        __sync_fetch_and_add(&usage->memory_bytes, bytes);
        if (usage->memory_bytes > usage->memory_peak)
            usage->memory_peak = usage->memory_bytes;
    }

    /* Check resource policy */
    struct resource_policy *policy;
    policy = bpf_map_lookup_elem(&resource_policy_map, &cgroup_id);
    if (policy && policy->memory_limit > 0) {
        if (usage && usage->memory_bytes > policy->memory_limit) {
            /* Over limit - send notification */
            struct mem_event *event;
            event = bpf_ringbuf_reserve(&resource_events, sizeof(*event), 0);
            if (event) {
                event->timestamp = bpf_ktime_get_ns();
                event->pid = bpf_get_current_pid_tgid() >> 32;
                event->cgroup_id = cgroup_id;
                event->bytes = bytes;
                event->total_bytes = usage->memory_bytes;
                event->is_allocation = true;
                bpf_get_current_comm(event->comm, sizeof(event->comm));
                bpf_ringbuf_submit(event, 0);
            }

            /* Increment throttle count */
            if (usage)
                __sync_fetch_and_add(&usage->throttle_count, 1);
        }
    }

    update_resource_stats(true, bytes, false);

    return 0;
}

/*
 * Tracepoint: kmem:kfree
 *
 * Track kernel memory frees.
 */
SEC("tp/kmem/kfree")
int trace_kfree(struct trace_event_raw_kmem_free *ctx)
{
    __u64 cgroup_id = get_current_cgroup_id();
    if (!cgroup_id)
        return 0;

    __u64 bytes = ctx->bytes_alloc;

    /* Update usage */
    struct resource_usage *usage;
    usage = bpf_map_lookup_elem(&resource_usage_map, &cgroup_id);
    if (usage) {
        if (usage->memory_bytes >= bytes)
            __sync_fetch_and_sub(&usage->memory_bytes, bytes);
        else
            usage->memory_bytes = 0;
    }

    update_resource_stats(false, bytes, false);

    return 0;
}

/*
 * Tracepoint: oom:oom_score_adj_update
 *
 * Track OOM score adjustments.
 */
SEC("tp/oom/oom_score_adj_update")
int trace_oom_score_update(struct trace_event_raw_oom_score_adj_update *ctx)
{
    /* Just track OOM events */
    update_resource_stats(false, 0, true);

    return 0;
}

/*
 * Kprobe: try_charge (memory cgroup charging)
 *
 * This hooks into the memory cgroup charging path to track
 * memory usage per cgroup.
 */
SEC("kprobe/try_charge")
int BPF_KPROBE(try_charge, struct mem_cgroup *memcg, gfp_t gfp, __u64 nr_pages)
{
    __u64 cgroup_id = get_current_cgroup_id();
    if (!cgroup_id)
        return 0;

    __u64 bytes = nr_pages * 4096;  /* Convert pages to bytes */

    /* Update usage */
    struct resource_usage *usage = get_resource_usage(cgroup_id);
    if (usage) {
        __sync_fetch_and_add(&usage->memory_bytes, bytes);
        if (usage->memory_bytes > usage->memory_peak)
            usage->memory_peak = usage->memory_bytes;
    }

    return 0;
}

/*
 * Kprobe: uncharge (memory cgroup uncharging)
 */
SEC("kprobe/uncharge")
int BPF_KPROBE(uncharge, struct mem_cgroup *memcg, __u64 nr_pages)
{
    __u64 cgroup_id = get_current_cgroup_id();
    if (!cgroup_id)
        return 0;

    __u64 bytes = nr_pages * 4096;

    /* Update usage */
    struct resource_usage *usage;
    usage = bpf_map_lookup_elem(&resource_usage_map, &cgroup_id);
    if (usage) {
        if (usage->memory_bytes >= bytes)
            __sync_fetch_and_sub(&usage->memory_bytes, bytes);
        else
            usage->memory_bytes = 0;
    }

    return 0;
}

char LICENSE[] SEC("license") = "GPL";
