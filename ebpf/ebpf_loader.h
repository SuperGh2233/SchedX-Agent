/* SPDX-License-Identifier: GPL-2.0 */
/*
 * ebpf_loader.h - Common eBPF loader framework
 *
 * This header provides a unified interface for loading and managing
 * different types of BPF programs in SchedX-Agent.
 */

#ifndef EBPF_LOADER_H
#define EBPF_LOADER_H

#include <bpf/libbpf.h>
#include <bpf/bpf.h>

/* BPF program types */
enum ebpf_prog_type {
    EBPF_PROG_SCHED_TRACE,     /* Scheduling latency tracer */
    EBPF_PROG_NET_POLICY,      /* Network policy enforcement */
    EBPF_PROG_RESOURCE_CTRL,   /* Resource control */
    EBPF_PROG_MAX,
};

/* BPF program configuration */
struct ebpf_config {
    enum ebpf_prog_type type;
    const char *name;
    const char *description;
    const char *bpf_object_path;
    bool auto_attach;
    bool dry_run;
};

/* BPF program state */
struct ebpf_prog_state {
    struct bpf_object *obj;
    struct bpf_link **links;
    int link_count;
    int map_fds[16];  /* File descriptors for key maps */
    int map_count;
    bool loaded;
    bool attached;
};

/* Scheduler trace statistics */
struct sched_trace_stats {
    __u64 total_wakeups;
    __u64 total_switches;
    __u64 total_latency_ns;
    __u32 active_tasks;
};

/* Network policy statistics */
struct net_policy_stats {
    __u64 total_packets;
    __u64 total_bytes;
    __u64 dropped_packets;
    __u64 dropped_bytes;
};

/* Resource control statistics */
struct resource_ctrl_stats {
    __u64 total_allocations;
    __u64 total_frees;
    __u64 total_bytes_allocated;
    __u64 total_bytes_freed;
    __u64 oom_events;
};

/* Loader API functions */

/**
 * ebpf_loader_init - Initialize the eBPF loader
 * @config: Configuration for the BPF program
 * @state: Output state structure
 *
 * Returns 0 on success, negative error code on failure.
 */
int ebpf_loader_init(const struct ebpf_config *config, struct ebpf_prog_state *state);

/**
 * ebpf_loader_load - Load a BPF program
 * @state: Program state
 *
 * Returns 0 on success, negative error code on failure.
 */
int ebpf_loader_load(struct ebpf_prog_state *state);

/**
 * ebpf_loader_attach - Attach BPF program to hooks
 * @state: Program state
 *
 * Returns 0 on success, negative error code on failure.
 */
int ebpf_loader_attach(struct ebpf_prog_state *state);

/**
 * ebpf_loader_detach - Detach BPF program from hooks
 * @state: Program state
 *
 * Returns 0 on success, negative error code on failure.
 */
int ebpf_loader_detach(struct ebpf_prog_state *state);

/**
 * ebpf_loader_destroy - Destroy BPF program and free resources
 * @state: Program state
 */
void ebpf_loader_destroy(struct ebpf_prog_state *state);

/**
 * ebpf_loader_get_stats - Get statistics from BPF program
 * @state: Program state
 * @stats: Output statistics buffer
 *
 * Returns 0 on success, negative error code on failure.
 */
int ebpf_loader_get_sched_trace_stats(struct ebpf_prog_state *state,
                                       struct sched_trace_stats *stats);
int ebpf_loader_get_net_policy_stats(struct ebpf_prog_state *state,
                                      struct net_policy_stats *stats);
int ebpf_loader_get_resource_ctrl_stats(struct ebpf_prog_state *state,
                                         struct resource_ctrl_stats *stats);

/* Map operations */

/**
 * ebpf_map_update_task_policy - Update task scheduling policy
 * @map_fd: Map file descriptor
 * @pid: Process ID
 * @class_id: Workload class
 * @weight: Scheduling weight
 *
 * Returns 0 on success, negative error code on failure.
 */
int ebpf_map_update_task_policy(int map_fd, __u32 pid, __u32 class_id, __u32 weight);

/**
 * ebpf_map_update_net_policy - Update network policy for a task
 * @map_fd: Map file descriptor
 * @pid: Process ID
 * @class_id: Network class
 * @rate_limit: Rate limit in bytes/sec
 * @burst_size: Burst size in bytes
 *
 * Returns 0 on success, negative error code on failure.
 */
int ebpf_map_update_net_policy(int map_fd, __u32 pid, __u32 class_id,
                               __u32 rate_limit, __u32 burst_size);

/**
 * ebpf_map_update_resource_policy - Update resource policy for a cgroup
 * @map_fd: Map file descriptor
 * @cgroup_id: Cgroup ID
 * @class_id: Resource class
 * @memory_limit: Memory limit in bytes
 * @cpu_weight: CPU weight
 * @io_weight: I/O weight
 *
 * Returns 0 on success, negative error code on failure.
 */
int ebpf_map_update_resource_policy(int map_fd, __u64 cgroup_id, __u32 class_id,
                                     __u64 memory_limit, __u32 cpu_weight, __u32 io_weight);

/* Utility functions */

/**
 * ebpf_check_support - Check if eBPF is supported on this system
 *
 * Returns true if eBPF is supported, false otherwise.
 */
bool ebpf_check_support(void);

/**
 * ebpf_check_sched_ext - Check if sched_ext is supported
 *
 * Returns true if sched_ext is supported, false otherwise.
 */
bool ebpf_check_sched_ext(void);

/**
 * ebpf_prog_type_name - Get human-readable name for program type
 * @type: Program type
 *
 * Returns pointer to static string with program type name.
 */
const char *ebpf_prog_type_name(enum ebpf_prog_type type);

#endif /* EBPF_LOADER_H */
