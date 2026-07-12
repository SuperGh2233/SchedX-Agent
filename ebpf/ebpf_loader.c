/* SPDX-License-Identifier: GPL-2.0 */
/*
 * ebpf_loader.c - eBPF loader implementation
 *
 * This implements the unified eBPF loading framework for SchedX-Agent.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>
#include "ebpf_loader.h"

/* BPF object file paths - adjust for your installation */
#define SCHED_TRACE_OBJ   "sched_trace.bpf.o"
#define NET_POLICY_OBJ    "net_policy.bpf.o"
#define RESOURCE_CTRL_OBJ "resource_ctrl.bpf.o"

/* Libbpf print callback */
static int libbpf_print_fn(enum libbpf_print_level level,
                            const char *format, va_list args)
{
    if (level == LIBBPF_DEBUG)
        return 0;
    return vfprintf(stderr, format, args);
}

/* Program type names */
static const char *prog_type_names[] = {
    [EBPF_PROG_SCHED_TRACE] = "sched_trace",
    [EBPF_PROG_NET_POLICY] = "net_policy",
    [EBPF_PROG_RESOURCE_CTRL] = "resource_ctrl",
};

const char *ebpf_prog_type_name(enum ebpf_prog_type type)
{
    if (type >= EBPF_PROG_MAX)
        return "unknown";
    return prog_type_names[type];
}

/* Check if eBPF is supported */
bool ebpf_check_support(void)
{
    /* Check if we can access BPF filesystem */
    if (access("/sys/fs/bpf", F_OK) != 0)
        return false;

    /* Check if we have BPF syscall support */
    int fd = bpf_map_create(BPF_MAP_TYPE_HASH, "test", sizeof(__u32),
                            sizeof(__u32), 1, NULL);
    if (fd < 0)
        return false;

    close(fd);
    return true;
}

/* Check if sched_ext is supported */
bool ebpf_check_sched_ext(void)
{
    struct stat st;
    return stat("/sys/kernel/sched_ext", &st) == 0;
}

/* Initialize eBPF loader */
int ebpf_loader_init(const struct ebpf_config *config, struct ebpf_prog_state *state)
{
    if (!config || !state)
        return -EINVAL;

    memset(state, 0, sizeof(*state));

    /* Set libbpf print callback */
    libbpf_set_print(libbpf_print_fn);

    return 0;
}

/* Load BPF program */
int ebpf_loader_load(struct ebpf_prog_state *state)
{
    if (!state)
        return -EINVAL;

    /* Open BPF object */
    state->obj = bpf_object__open(state->bpf_object_path);
    if (libbpf_get_error(state->obj)) {
        fprintf(stderr, "Failed to open BPF object: %s\n",
                libbpf_get_error_str());
        return -ENOENT;
    }

    /* Load BPF program */
    int err = bpf_object__load(state->obj);
    if (err) {
        fprintf(stderr, "Failed to load BPF program: %s\n", strerror(-err));
        bpf_object__close(state->obj);
        state->obj = NULL;
        return err;
    }

    state->loaded = true;
    return 0;
}

/* Attach BPF program to hooks */
int ebpf_loader_attach(struct ebpf_prog_state *state)
{
    if (!state || !state->obj)
        return -EINVAL;

    struct bpf_program *prog;
    struct bpf_link *link;
    int link_count = 0;

    /* Count programs */
    bpf_object__for_each_program(prog, state->obj) {
        link_count++;
    }

    /* Allocate links array */
    state->links = calloc(link_count, sizeof(struct bpf_link *));
    if (!state->links)
        return -ENOMEM;

    /* Attach each program */
    int i = 0;
    bpf_object__for_each_program(prog, state->obj) {
        link = bpf_program__attach(prog);
        if (libbpf_get_error(link)) {
            fprintf(stderr, "Failed to attach program %s: %s\n",
                    bpf_program__name(prog), libbpf_get_error_str());
            /* Cleanup already attached links */
            for (int j = 0; j < i; j++)
                bpf_link__destroy(state->links[j]);
            free(state->links);
            state->links = NULL;
            return -EINVAL;
        }
        state->links[i++] = link;
    }

    state->link_count = link_count;
    state->attached = true;

    return 0;
}

/* Detach BPF program from hooks */
int ebpf_loader_detach(struct ebpf_prog_state *state)
{
    if (!state || !state->attached)
        return -EINVAL;

    for (int i = 0; i < state->link_count; i++) {
        if (state->links[i])
            bpf_link__destroy(state->links[i]);
    }

    free(state->links);
    state->links = NULL;
    state->link_count = 0;
    state->attached = false;

    return 0;
}

/* Destroy BPF program and free resources */
void ebpf_loader_destroy(struct ebpf_prog_state *state)
{
    if (!state)
        return;

    if (state->attached)
        ebpf_loader_detach(state);

    if (state->obj) {
        bpf_object__close(state->obj);
        state->obj = NULL;
    }

    state->loaded = false;
}

/* Get scheduling trace statistics */
int ebpf_loader_get_sched_trace_stats(struct ebpf_prog_state *state,
                                       struct sched_trace_stats *stats)
{
    if (!state || !stats || !state->loaded)
        return -EINVAL;

    /* Find global_stats map */
    int map_fd = bpf_object__find_map_fd_by_name(state->obj, "global_stats");
    if (map_fd < 0)
        return -ENOENT;

    /* Read per-CPU stats and aggregate */
    int nr_cpus = libbpf_num_possible_cpus();
    struct {
        __u64 total_wakeups;
        __u64 total_switches;
        __u64 total_latency;
        __u32 active_tasks;
    } *per_cpu_stats;

    per_cpu_stats = calloc(nr_cpus, sizeof(*per_cpu_stats));
    if (!per_cpu_stats)
        return -ENOMEM;

    __u32 key = 0;
    int err = bpf_map_lookup_elem(map_fd, &key, per_cpu_stats);
    if (err) {
        free(per_cpu_stats);
        return err;
    }

    /* Aggregate */
    memset(stats, 0, sizeof(*stats));
    for (int i = 0; i < nr_cpus; i++) {
        stats->total_wakeups += per_cpu_stats[i].total_wakeups;
        stats->total_switches += per_cpu_stats[i].total_switches;
        stats->total_latency_ns += per_cpu_stats[i].total_latency;
        stats->active_tasks += per_cpu_stats[i].active_tasks;
    }

    free(per_cpu_stats);
    return 0;
}

/* Get network policy statistics */
int ebpf_loader_get_net_policy_stats(struct ebpf_prog_state *state,
                                      struct net_policy_stats *stats)
{
    if (!state || !stats || !state->loaded)
        return -EINVAL;

    int map_fd = bpf_object__find_map_fd_by_name(state->obj, "global_net_stats_map");
    if (map_fd < 0)
        return -ENOENT;

    int nr_cpus = libbpf_num_possible_cpus();
    struct {
        __u64 total_packets;
        __u64 total_bytes;
        __u64 dropped_packets;
        __u64 dropped_bytes;
    } *per_cpu_stats;

    per_cpu_stats = calloc(nr_cpus, sizeof(*per_cpu_stats));
    if (!per_cpu_stats)
        return -ENOMEM;

    __u32 key = 0;
    int err = bpf_map_lookup_elem(map_fd, &key, per_cpu_stats);
    if (err) {
        free(per_cpu_stats);
        return err;
    }

    memset(stats, 0, sizeof(*stats));
    for (int i = 0; i < nr_cpus; i++) {
        stats->total_packets += per_cpu_stats[i].total_packets;
        stats->total_bytes += per_cpu_stats[i].total_bytes;
        stats->dropped_packets += per_cpu_stats[i].dropped_packets;
        stats->dropped_bytes += per_cpu_stats[i].dropped_bytes;
    }

    free(per_cpu_stats);
    return 0;
}

/* Get resource control statistics */
int ebpf_loader_get_resource_ctrl_stats(struct ebpf_prog_state *state,
                                         struct resource_ctrl_stats *stats)
{
    if (!state || !stats || !state->loaded)
        return -EINVAL;

    int map_fd = bpf_object__find_map_fd_by_name(state->obj, "global_resource_stats_map");
    if (map_fd < 0)
        return -ENOENT;

    int nr_cpus = libbpf_num_possible_cpus();
    struct {
        __u64 total_allocations;
        __u64 total_frees;
        __u64 total_bytes_allocated;
        __u64 total_bytes_freed;
        __u64 oom_events;
        __u64 throttle_events;
    } *per_cpu_stats;

    per_cpu_stats = calloc(nr_cpus, sizeof(*per_cpu_stats));
    if (!per_cpu_stats)
        return -ENOMEM;

    __u32 key = 0;
    int err = bpf_map_lookup_elem(map_fd, &key, per_cpu_stats);
    if (err) {
        free(per_cpu_stats);
        return err;
    }

    memset(stats, 0, sizeof(*stats));
    for (int i = 0; i < nr_cpus; i++) {
        stats->total_allocations += per_cpu_stats[i].total_allocations;
        stats->total_frees += per_cpu_stats[i].total_frees;
        stats->total_bytes_allocated += per_cpu_stats[i].total_bytes_allocated;
        stats->total_bytes_freed += per_cpu_stats[i].total_bytes_freed;
        stats->oom_events += per_cpu_stats[i].oom_events;
    }

    free(per_cpu_stats);
    return 0;
}

/* Update task policy in BPF map */
int ebpf_map_update_task_policy(int map_fd, __u32 pid, __u32 class_id, __u32 weight)
{
    struct {
        __u32 class_id;
        __u32 weight;
    } policy = {
        .class_id = class_id,
        .weight = weight,
    };

    return bpf_map_update_elem(map_fd, &pid, &policy, BPF_ANY);
}

/* Update network policy in BPF map */
int ebpf_map_update_net_policy(int map_fd, __u32 pid, __u32 class_id,
                               __u32 rate_limit, __u32 burst_size)
{
    struct {
        __u32 class_id;
        __u32 rate_limit_bps;
        __u32 burst_size;
        __u32 priority;
    } policy = {
        .class_id = class_id,
        .rate_limit_bps = rate_limit,
        .burst_size = burst_size,
        .priority = (class_id == 1) ? 7 : (class_id == 2) ? 4 : 1,
    };

    return bpf_map_update_elem(map_fd, &pid, &policy, BPF_ANY);
}

/* Update resource policy in BPF map */
int ebpf_map_update_resource_policy(int map_fd, __u64 cgroup_id, __u32 class_id,
                                     __u64 memory_limit, __u32 cpu_weight, __u32 io_weight)
{
    struct {
        __u32 class_id;
        __u64 memory_limit;
        __u32 cpu_weight;
        __u32 io_weight;
    } policy = {
        .class_id = class_id,
        .memory_limit = memory_limit,
        .cpu_weight = cpu_weight,
        .io_weight = io_weight,
    };

    return bpf_map_update_elem(map_fd, &cgroup_id, &policy, BPF_ANY);
}
