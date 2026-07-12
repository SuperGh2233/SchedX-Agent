/* SPDX-License-Identifier: GPL-2.0 */
/*
 * sched_trace.bpf.c - Scheduling latency tracer using eBPF
 *
 * This BPF program traces scheduling events (sched_switch, sched_wakeup)
 * and collects latency statistics for workload analysis.
 *
 * Features:
 * - Tracks scheduling latency per task
 * - Collects context switch statistics
 * - Identifies latency-sensitive vs batch workloads
 * - Provides histograms for latency distribution
 */

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

/* Maximum number of tasks to track */
#define MAX_TASKS        8192
#define MAX_LATENCY_BUCKETS 32

/* Latency bucket boundaries in nanoseconds */
#define BUCKET_100NS     100
#define BUCKET_1US       1000
#define BUCKET_10US      10000
#define BUCKET_100US     100000
#define BUCKET_1MS       1000000
#define BUCKET_10MS      10000000
#define BUCKET_100MS     100000000
#define BUCKET_1S        1000000000

/* Task information structure */
struct task_info {
    __u64 wakeup_time;      /* Last wakeup timestamp */
    __u64 switch_time;      /* Last context switch timestamp */
    __u64 total_latency;    /* Cumulative scheduling latency */
    __u64 total_runtime;    /* Cumulative runtime */
    __u32 wakeup_count;     /* Number of wakeups */
    __u32 switch_count;     /* Number of context switches */
    __u32 pid;              /* Process ID */
    char comm[16];          /* Process name */
};

/* Latency histogram bucket */
struct latency_bucket {
    __u64 count;
    __u64 total_ns;
};

/* Global statistics */
struct sched_stats {
    __u64 total_wakeups;
    __u64 total_switches;
    __u64 total_latency;
    __u32 active_tasks;
};

/* BPF Maps */

/* Per-task tracking information */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, MAX_TASKS);
    __type(key, __u32);           /* PID */
    __type(value, struct task_info);
} task_info_map SEC(".maps");

/* Latency histogram (per-CPU) */
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, MAX_LATENCY_BUCKETS);
    __type(key, __u32);
    __type(value, struct latency_bucket);
} latency_histogram SEC(".maps");

/* Global statistics (per-CPU) */
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct sched_stats);
} global_stats SEC(".maps");

/* Ring buffer for real-time events */
struct sched_event {
    __u64 timestamp;
    __u32 pid;
    __u32 prev_pid;
    __u64 latency_ns;
    __u64 runtime_ns;
    char comm[16];
    char prev_comm[16];
};

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);  /* 256KB */
} events SEC(".maps");

/* Helper: Update latency histogram */
static __always_inline void update_histogram(__u64 latency_ns)
{
    __u32 bucket;

    if (latency_ns < BUCKET_100NS)
        bucket = 0;
    else if (latency_ns < BUCKET_1US)
        bucket = 1;
    else if (latency_ns < BUCKET_10US)
        bucket = 2;
    else if (latency_ns < BUCKET_100US)
        bucket = 3;
    else if (latency_ns < BUCKET_1MS)
        bucket = 4;
    else if (latency_ns < BUCKET_10MS)
        bucket = 5;
    else if (latency_ns < BUCKET_100MS)
        bucket = 6;
    else if (latency_ns < BUCKET_1S)
        bucket = 7;
    else
        bucket = 8;

    struct latency_bucket *entry = bpf_map_lookup_elem(&latency_histogram, &bucket);
    if (entry) {
        entry->count++;
        entry->total_ns += latency_ns;
    }
}

/* Helper: Update global statistics */
static __always_inline void update_stats(bool is_wakeup, __u64 latency_ns)
{
    __u32 key = 0;
    struct sched_stats *stats = bpf_map_lookup_elem(&global_stats, &key);
    if (!stats)
        return;

    if (is_wakeup) {
        stats->total_wakeups++;
        stats->total_latency += latency_ns;
    } else {
        stats->total_switches++;
    }
}

/* Helper: Get or create task info */
static __always_inline struct task_info *get_task_info(__u32 pid)
{
    struct task_info *info;

    info = bpf_map_lookup_elem(&task_info_map, &pid);
    if (info)
        return info;

    /* Create new entry */
    struct task_info new_info = {
        .pid = pid,
    };
    bpf_map_update_elem(&task_info_map, &pid, &new_info, BPF_ANY);
    return bpf_map_lookup_elem(&task_info_map, &pid);
}

/*
 * Tracepoint: sched:sched_wakeup
 *
 * Called when a task is woken up. Records the wakeup timestamp.
 */
SEC("tp/sched/sched_wakeup")
int trace_sched_wakeup(struct trace_event_raw_sched_wakeup_template *ctx)
{
    __u32 pid = ctx->pid;
    __u64 now = bpf_ktime_get_ns();

    struct task_info *info = get_task_info(pid);
    if (!info)
        return 0;

    info->wakeup_time = now;
    info->wakeup_count++;

    /* Read task comm */
    bpf_get_current_comm(info->comm, sizeof(info->comm));

    return 0;
}

/*
 * Tracepoint: sched:sched_switch
 *
 * Called on context switch. Calculates scheduling latency for the incoming task.
 */
SEC("tp/sched/sched_switch")
int trace_sched_switch(struct trace_event_raw_sched_switch *ctx)
{
    __u64 now = bpf_ktime_get_ns();

    /* Get incoming task info */
    __u32 next_pid = ctx->next_pid;
    struct task_info *next_info = get_task_info(next_pid);
    if (!next_info)
        return 0;

    /* Calculate scheduling latency (time from wakeup to running) */
    if (next_info->wakeup_time > 0) {
        __u64 latency = now - next_info->wakeup_time;

        /* Sanity check */
        if (latency < 10000000000ULL) {  /* Less than 10 seconds */
            next_info->total_latency += latency;
            update_histogram(latency);
            update_stats(true, latency);
        }

        next_info->wakeup_time = 0;
    }

    /* Update switch count and time */
    next_info->switch_count++;
    next_info->switch_time = now;

    /* Get outgoing task runtime */
    __u32 prev_pid = ctx->prev_pid;
    struct task_info *prev_info = bpf_map_lookup_elem(&task_info_map, &prev_pid);
    if (prev_info && prev_info->switch_time > 0) {
        __u64 runtime = now - prev_info->switch_time;
        prev_info->total_runtime += runtime;
    }

    /* Send event to ring buffer (sample 1 in 100 to reduce overhead) */
    if (bpf_get_prandom_u32() % 100 == 0) {
        struct sched_event *event;
        event = bpf_ringbuf_reserve(&events, sizeof(*event), 0);
        if (event) {
            event->timestamp = now;
            event->pid = next_pid;
            event->prev_pid = prev_pid;
            event->latency_ns = (next_info->wakeup_time > 0) ?
                                (now - next_info->wakeup_time) : 0;
            event->runtime_ns = (prev_info && prev_info->switch_time > 0) ?
                                (now - prev_info->switch_time) : 0;
            bpf_probe_read_kernel_str(event->comm, sizeof(event->comm),
                                      ctx->next_comm);
            bpf_probe_read_kernel_str(event->prev_comm, sizeof(event->prev_comm),
                                      ctx->prev_comm);
            bpf_ringbuf_submit(event, 0);
        }
    }

    /* Update global stats */
    update_stats(false, 0);

    return 0;
}

/*
 * Tracepoint: sched:sched_wakeup_new
 *
 * Called when a new task is created and woken up.
 */
SEC("tp/sched/sched_wakeup_new")
int trace_sched_wakeup_new(struct trace_event_raw_sched_wakeup_template *ctx)
{
    __u32 pid = ctx->pid;
    __u64 now = bpf_ktime_get_ns();

    struct task_info new_info = {
        .pid = pid,
        .wakeup_time = now,
        .wakeup_count = 1,
    };
    bpf_get_current_comm(new_info.comm, sizeof(new_info.comm));

    bpf_map_update_elem(&task_info_map, &pid, &new_info, BPF_ANY);

    return 0;
}

/*
 * Tracepoint: sched:sched_process_exit
 *
 * Called when a task exits. Cleans up task info.
 */
SEC("tp/sched/sched_process_exit")
int trace_sched_process_exit(struct trace_event_raw_sched_process_template *ctx)
{
    __u32 pid = ctx->pid;

    /* Don't delete immediately - let userspace read the data first */
    /* In production, you'd want a more sophisticated cleanup mechanism */

    return 0;
}

char LICENSE[] SEC("license") = "GPL";
