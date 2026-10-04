// SPDX-License-Identifier: GPL-2.0
/*
 * SchedX native sched_ext scheduler.
 *
 * Tasks are placed into class-specific dispatch queues. Dispatch always
 * prefers latency-sensitive work, then regular/batch work, then background
 * work. Policies are updated from userspace through task_policy_map.
 */

#include <scx/common.bpf.h>
#include "scx_agent_common.h"

char _license[] SEC("license") = "GPL";

#define DSQ_LATENCY    0
#define DSQ_DEFAULT    1
#define DSQ_BACKGROUND 2
#define DSQ_BATCH      3
#define DSQ_COUNT      4
#define WEIGHT_BASE    1000
#define WEIGHT_MIN     1
#define WEIGHT_MAX     10000
#define SLICE_MIN      (SCX_SLICE_DFL / 10)
#define SLICE_MAX      (SCX_SLICE_DFL * 4)
#define LATENCY_SLICE_MAX (SCX_SLICE_DFL / 2)

UEI_DEFINE(uei);
static u64 dsq_vtime_now[DSQ_COUNT];

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 65536);
	__type(key, u32);
	__type(value, struct schedx_policy);
} task_policy_map SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 4096);
	__type(key, u64);
	__type(value, struct schedx_policy);
} cgroup_policy_map SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 4096);
	__type(key, u64);
	__type(value, struct schedx_cgroup_metrics);
} cgroup_metrics_map SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct schedx_config);
} scheduler_config SEC(".maps");

struct schedx_dispatch_ctx {
	u32 since_background;
	u32 since_default;
	u32 since_batch;
};

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct schedx_dispatch_ctx);
} dispatch_ctx SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, SCX_CLASS_COUNT);
	__type(key, u32);
	__type(value, u64);
} dispatch_stats SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, SCX_CLASS_COUNT);
	__type(key, u32);
	__type(value, struct schedx_class_metrics);
} class_metrics_map SEC(".maps");

struct schedx_task_ctx {
	u64 started_at;
	u64 queued_at;
	u64 cgroup_id;
	u32 class_id;
};

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct schedx_task_ctx);
} task_ctx_stor SEC(".maps");

static void stat_inc(u32 class_id)
{
	u64 *count;

	if (class_id >= SCX_CLASS_COUNT)
		class_id = SCX_CLASS_UNKNOWN;
	count = bpf_map_lookup_elem(&dispatch_stats, &class_id);
	if (count)
		(*count)++;
}

static struct schedx_policy *task_policy(struct task_struct *p)
{
	u32 pid = p->pid;
	struct schedx_policy *policy;
	u64 cgroup_id;

	policy = bpf_map_lookup_elem(&task_policy_map, &pid);
	if (policy)
		return policy;
	cgroup_id = BPF_CORE_READ(p, cgroups, dfl_cgrp, kn, id);
	return bpf_map_lookup_elem(&cgroup_policy_map, &cgroup_id);
}

static u64 task_cgroup_id(struct task_struct *p)
{
	return BPF_CORE_READ(p, cgroups, dfl_cgrp, kn, id);
}

static struct schedx_cgroup_metrics *task_metrics(struct task_struct *p, u64 *cgroup_id)
{
	*cgroup_id = task_cgroup_id(p);
	return bpf_map_lookup_elem(&cgroup_metrics_map, cgroup_id);
}

static u32 task_class(struct task_struct *p)
{
	struct schedx_policy *policy = task_policy(p);

	if (!policy || policy->class_id >= SCX_CLASS_COUNT)
		return SCX_CLASS_UNKNOWN;
	return policy->class_id;
}

static u32 task_weight(struct task_struct *p)
{
	struct schedx_policy *policy = task_policy(p);
	if (!policy || policy->weight < WEIGHT_MIN ||
	    policy->weight > WEIGHT_MAX)
		return WEIGHT_BASE;
	return policy->weight;
}

static u64 task_slice(struct task_struct *p, u32 class_id)
{
	u64 slice = SCX_SLICE_DFL * task_weight(p) / WEIGHT_BASE;

	if (slice < SLICE_MIN)
		slice = SLICE_MIN;
	if (slice > SLICE_MAX)
		slice = SLICE_MAX;
	if (class_id == SCX_CLASS_LATENCY && slice > LATENCY_SLICE_MAX)
		slice = LATENCY_SLICE_MAX;
	return slice;
}

static u64 class_dsq(u32 class_id)
{
	switch (class_id) {
	case SCX_CLASS_LATENCY:
		return DSQ_LATENCY;
	case SCX_CLASS_BACKGROUND:
		return DSQ_BACKGROUND;
	case SCX_CLASS_BATCH:
		return DSQ_BATCH;
	default:
		return DSQ_DEFAULT;
	}
}

s32 BPF_STRUCT_OPS(schedx_select_cpu, struct task_struct *p, s32 prev_cpu,
		   u64 wake_flags)
{
	bool is_idle = false;

	/*
	 * Always enqueue through a class DSQ. Direct local insertion would bypass
	 * both class priority and weighted-vtime ordering.
	 */
	return scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
}

void BPF_STRUCT_OPS(schedx_enqueue, struct task_struct *p, u64 enq_flags)
{
	u32 class_id = task_class(p);
	u64 dsq_id = class_dsq(class_id);
	u64 slice = task_slice(p, class_id);
	u64 vtime = p->scx.dsq_vtime;
	u64 idle_credit = SCX_SLICE_DFL * WEIGHT_BASE / task_weight(p);
	u64 cgroup_id;
	struct schedx_cgroup_metrics *metrics;
	struct schedx_task_ctx *taskc;

	stat_inc(class_id);
	metrics = task_metrics(p, &cgroup_id);
	if (metrics)
		__sync_fetch_and_add(&metrics->enqueues, 1);
	taskc = bpf_task_storage_get(&task_ctx_stor, p, 0,
					     BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (taskc) {
		if (taskc->class_id != class_id) {
			vtime = dsq_vtime_now[dsq_id];
			p->scx.dsq_vtime = vtime;
		}
		taskc->queued_at = bpf_ktime_get_ns();
		taskc->cgroup_id = cgroup_id;
		taskc->class_id = class_id;
	}
	struct schedx_class_metrics *classm = bpf_map_lookup_elem(&class_metrics_map, &class_id);
	if (classm)
		classm->enqueues++;
	/* Sleep-heavy peers must not repeatedly overtake latency or control work.
	 * Use round-robin order and weight-scaled, capped slices for these classes.
	 */
	if (class_id == SCX_CLASS_LATENCY || class_id == SCX_CLASS_UNKNOWN) {
		scx_bpf_dsq_insert(p, dsq_id, slice, enq_flags);
	} else {
		/* Vtime is weight-scaled, so limit idle credit in the same units. */
		if (time_before(vtime, dsq_vtime_now[dsq_id] - idle_credit))
			vtime = dsq_vtime_now[dsq_id] - idle_credit;
		scx_bpf_dsq_insert_vtime(p, dsq_id, slice, vtime, enq_flags);
	}
}

void BPF_STRUCT_OPS(schedx_dispatch, s32 cpu, struct task_struct *prev)
{
	u32 zero = 0;
	struct schedx_dispatch_ctx *dctx = bpf_map_lookup_elem(&dispatch_ctx, &zero);
	struct schedx_config *config = bpf_map_lookup_elem(&scheduler_config, &zero);
	u64 selected;

	/* Give each non-latency class its own bounded service opportunity. */
	if (dctx && config && config->default_interval &&
	    dctx->since_batch >= config->default_interval &&
	    scx_bpf_dsq_move_to_local(DSQ_BATCH)) {
		selected = DSQ_BATCH;
		goto accounted;
	}
	if (dctx && config && config->default_interval &&
	    dctx->since_default >= config->default_interval &&
	    scx_bpf_dsq_move_to_local(DSQ_DEFAULT)) {
		selected = DSQ_DEFAULT;
		goto accounted;
	}
	if (dctx && config && config->background_interval &&
	    dctx->since_background >= config->background_interval &&
	    scx_bpf_dsq_move_to_local(DSQ_BACKGROUND)) {
		selected = DSQ_BACKGROUND;
		goto accounted;
	}
	if (scx_bpf_dsq_move_to_local(DSQ_LATENCY))
		selected = DSQ_LATENCY;
	else if (scx_bpf_dsq_move_to_local(DSQ_DEFAULT))
		selected = DSQ_DEFAULT;
	else if (scx_bpf_dsq_move_to_local(DSQ_BATCH))
		selected = DSQ_BATCH;
	else if (scx_bpf_dsq_move_to_local(DSQ_BACKGROUND))
		selected = DSQ_BACKGROUND;
	else
		return;
accounted:
	if (dctx) {
		dctx->since_background = selected == DSQ_BACKGROUND ? 0 : dctx->since_background + 1;
		dctx->since_default = selected == DSQ_DEFAULT ? 0 : dctx->since_default + 1;
		dctx->since_batch = selected == DSQ_BATCH ? 0 : dctx->since_batch + 1;
	}
}

void BPF_STRUCT_OPS(schedx_running, struct task_struct *p)
{
	u64 dsq_id = class_dsq(task_class(p));
	struct schedx_task_ctx *taskc;

	if (time_before(dsq_vtime_now[dsq_id], p->scx.dsq_vtime))
		dsq_vtime_now[dsq_id] = p->scx.dsq_vtime;
	taskc = bpf_task_storage_get(&task_ctx_stor, p, 0,
				     BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (taskc)
	{
		struct schedx_cgroup_metrics *metrics;
		u64 now = bpf_ktime_get_ns();

		metrics = bpf_map_lookup_elem(&cgroup_metrics_map,
					      &taskc->cgroup_id);
		if (metrics) {
			__sync_fetch_and_add(&metrics->runs, 1);
			if (taskc->queued_at && now > taskc->queued_at)
				__sync_fetch_and_add(&metrics->wait_ns,
						     now - taskc->queued_at);
		}
		struct schedx_class_metrics *classm = bpf_map_lookup_elem(&class_metrics_map, &taskc->class_id);
		if (classm) {
			u64 wait = taskc->queued_at && now > taskc->queued_at ? now - taskc->queued_at : 0;
			classm->runs++;
			classm->wait_ns += wait;
			if (wait > classm->max_wait_ns)
				classm->max_wait_ns = wait;
		}
		taskc->queued_at = 0;
		taskc->started_at = now;
	}
}

void BPF_STRUCT_OPS(schedx_stopping, struct task_struct *p, bool runnable)
{
	struct schedx_task_ctx *taskc;
	u32 class_id = task_class(p);
	u64 assigned_slice = task_slice(p, class_id);
	u64 slice_used = assigned_slice > p->scx.slice ?
		assigned_slice - p->scx.slice : 0;
	u64 runtime_used = slice_used;
	u32 weight = task_weight(p);

	taskc = bpf_task_storage_get(&task_ctx_stor, p, 0, 0);
	if (taskc && taskc->started_at) {
		struct schedx_cgroup_metrics *metrics;

		runtime_used = bpf_ktime_get_ns() - taskc->started_at;
		taskc->started_at = 0;
		metrics = bpf_map_lookup_elem(&cgroup_metrics_map,
						      &taskc->cgroup_id);
		if (metrics)
			__sync_fetch_and_add(&metrics->runtime_ns, runtime_used);
		struct schedx_class_metrics *classm = bpf_map_lookup_elem(&class_metrics_map, &taskc->class_id);
		if (classm)
			classm->runtime_ns += runtime_used;
	}

	/* Charge measured execution even when the kernel replenishes the slice.
	 * Keep slice accounting as a fallback if task storage is unavailable.
	 */
	p->scx.dsq_vtime += runtime_used * WEIGHT_BASE / weight;
}

void BPF_STRUCT_OPS(schedx_enable, struct task_struct *p)
{
	p->scx.dsq_vtime = dsq_vtime_now[class_dsq(task_class(p))];
}

s32 BPF_STRUCT_OPS_SLEEPABLE(schedx_init)
{
	s32 ret;

	ret = scx_bpf_create_dsq(DSQ_LATENCY, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(DSQ_DEFAULT, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(DSQ_BACKGROUND, -1);
	if (ret)
		return ret;
	return scx_bpf_create_dsq(DSQ_BATCH, -1);
}

void BPF_STRUCT_OPS(schedx_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(schedx_ops,
	       .select_cpu	= (void *)schedx_select_cpu,
	       .enqueue		= (void *)schedx_enqueue,
	       .dispatch		= (void *)schedx_dispatch,
	       .running		= (void *)schedx_running,
	       .stopping		= (void *)schedx_stopping,
	       .enable		= (void *)schedx_enable,
	       .init		= (void *)schedx_init,
	       .exit		= (void *)schedx_exit,
	       .name		= "schedx_agent");
