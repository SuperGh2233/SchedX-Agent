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
#define DSQ_COUNT      3
#define WEIGHT_BASE    1000
#define WEIGHT_MIN     1
#define WEIGHT_MAX     10000

UEI_DEFINE(uei);
static u64 dsq_vtime_now[DSQ_COUNT];

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 65536);
	__type(key, u32);
	__type(value, struct schedx_policy);
} task_policy_map SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, SCX_CLASS_COUNT);
	__type(key, u32);
	__type(value, u64);
} dispatch_stats SEC(".maps");

static void stat_inc(u32 class_id)
{
	u64 *count;

	if (class_id >= SCX_CLASS_COUNT)
		class_id = SCX_CLASS_UNKNOWN;
	count = bpf_map_lookup_elem(&dispatch_stats, &class_id);
	if (count)
		(*count)++;
}

static u32 task_class(struct task_struct *p)
{
	u32 pid = p->pid;
	struct schedx_policy *policy;

	policy = bpf_map_lookup_elem(&task_policy_map, &pid);
	if (!policy || policy->class_id >= SCX_CLASS_COUNT)
		return SCX_CLASS_UNKNOWN;
	return policy->class_id;
}

static u32 task_weight(struct task_struct *p)
{
	u32 pid = p->pid;
	struct schedx_policy *policy;

	policy = bpf_map_lookup_elem(&task_policy_map, &pid);
	if (!policy || policy->weight < WEIGHT_MIN ||
	    policy->weight > WEIGHT_MAX)
		return WEIGHT_BASE;
	return policy->weight;
}

static u64 class_dsq(u32 class_id)
{
	switch (class_id) {
	case SCX_CLASS_LATENCY:
		return DSQ_LATENCY;
	case SCX_CLASS_BACKGROUND:
		return DSQ_BACKGROUND;
	default:
		return DSQ_DEFAULT;
	}
}

static u64 class_slice(u32 class_id)
{
	switch (class_id) {
	case SCX_CLASS_LATENCY:
		return SCX_SLICE_DFL / 2;
	case SCX_CLASS_BATCH:
		return SCX_SLICE_DFL * 2;
	case SCX_CLASS_BACKGROUND:
		return SCX_SLICE_DFL * 4;
	default:
		return SCX_SLICE_DFL;
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
	u64 vtime = p->scx.dsq_vtime;

	stat_inc(class_id);
	if (time_before(vtime, dsq_vtime_now[dsq_id] - SCX_SLICE_DFL))
		vtime = dsq_vtime_now[dsq_id] - SCX_SLICE_DFL;
	scx_bpf_dsq_insert_vtime(p, dsq_id, class_slice(class_id), vtime,
				 enq_flags);
}

void BPF_STRUCT_OPS(schedx_dispatch, s32 cpu, struct task_struct *prev)
{
	if (scx_bpf_dsq_move_to_local(DSQ_LATENCY))
		return;
	if (scx_bpf_dsq_move_to_local(DSQ_DEFAULT))
		return;
	scx_bpf_dsq_move_to_local(DSQ_BACKGROUND);
}

void BPF_STRUCT_OPS(schedx_running, struct task_struct *p)
{
	u64 dsq_id = class_dsq(task_class(p));

	if (time_before(dsq_vtime_now[dsq_id], p->scx.dsq_vtime))
		dsq_vtime_now[dsq_id] = p->scx.dsq_vtime;
}

void BPF_STRUCT_OPS(schedx_stopping, struct task_struct *p, bool runnable)
{
	u64 slice = class_slice(task_class(p));
	u64 used = p->scx.slice < slice ? slice - p->scx.slice : 0;
	u32 weight = task_weight(p);

	p->scx.dsq_vtime += used * WEIGHT_BASE / weight;
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
	return scx_bpf_create_dsq(DSQ_BACKGROUND, -1);
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
