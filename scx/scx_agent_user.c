// SPDX-License-Identifier: GPL-2.0
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <bpf/bpf.h>
#include <scx/common.h>
#include "scx_agent.bpf.skel.h"
#include "scx_agent_common.h"

static volatile sig_atomic_t exit_req;

static const char *class_name(__u32 id)
{
	switch (id) {
	case SCX_CLASS_LATENCY: return "latency";
	case SCX_CLASS_BATCH: return "batch";
	case SCX_CLASS_BACKGROUND: return "background";
	default: return "unknown";
	}
}

static void signal_handler(int sig)
{
	exit_req = 1;
	/*
	 * close() is async-signal-safe and wakes the blocking fgets() below so
	 * SIGTERM always unregisters the struct_ops link promptly.
	 */
	close(STDIN_FILENO);
}

static void print_prompt(void)
{
	/* A line-delimited prompt keeps the Python controller protocol reliable. */
	printf("schedx>\n");
	fflush(stdout);
}

static void print_stats(struct scx_agent *skel)
{
	int cpus = libbpf_num_possible_cpus();
	__u64 values[cpus];
	__u64 totals[SCX_CLASS_COUNT] = {};
	__u32 class_id;
	int cpu;

	for (class_id = 0; class_id < SCX_CLASS_COUNT; class_id++) {
		memset(values, 0, sizeof(values));
		if (!bpf_map_lookup_elem(bpf_map__fd(skel->maps.dispatch_stats),
					 &class_id, values))
			for (cpu = 0; cpu < cpus; cpu++)
				totals[class_id] += values[cpu];
	}

	printf("Default dispatches: %llu\n", totals[SCX_CLASS_UNKNOWN]);
	printf("Latency dispatches: %llu\n", totals[SCX_CLASS_LATENCY]);
	printf("Batch dispatches: %llu\n", totals[SCX_CLASS_BATCH]);
	printf("Background dispatches: %llu\n", totals[SCX_CLASS_BACKGROUND]);
}

static int set_task_policy(struct scx_agent *skel, __u32 pid, __u32 class_id,
			   __u32 weight)
{
	struct schedx_policy policy = {
		.class_id = class_id,
		.weight = weight,
	};

	if (class_id >= SCX_CLASS_COUNT || weight < 1 || weight > 10000)
		return -EINVAL;
	return bpf_map_update_elem(bpf_map__fd(skel->maps.task_policy_map), &pid,
				   &policy, BPF_ANY);
}

static int set_cgroup_policy(struct scx_agent *skel, __u64 cgroup_id,
			     __u32 class_id, __u32 weight)
{
	struct schedx_policy policy = {
		.class_id = class_id,
		.weight = weight,
	};
	struct schedx_cgroup_metrics metrics = {};
	int ret;

	if (class_id >= SCX_CLASS_COUNT || weight < 1 || weight > 10000)
		return -EINVAL;
	ret = bpf_map_update_elem(bpf_map__fd(skel->maps.cgroup_policy_map),
				  &cgroup_id, &policy, BPF_ANY);
	if (ret)
		return ret;
	ret = bpf_map_update_elem(bpf_map__fd(skel->maps.cgroup_metrics_map),
				  &cgroup_id, &metrics, BPF_NOEXIST);
	return ret && errno != EEXIST ? ret : 0;
}

static int set_fairness(struct scx_agent *skel, __u32 background_interval,
			__u32 default_interval)
{
	__u32 key = 0;
	struct schedx_config config = {
		.background_interval = background_interval,
		.default_interval = default_interval,
	};

	return bpf_map_update_elem(bpf_map__fd(skel->maps.scheduler_config),
				   &key, &config, BPF_ANY);
}

static void dump_policies(struct scx_agent *skel)
{
	int fd = bpf_map__fd(skel->maps.task_policy_map);
	int cgroup_fd = bpf_map__fd(skel->maps.cgroup_policy_map);
	__u32 key, next_key;
	__u64 cgroup_key, next_cgroup_key;
	struct schedx_policy policy;
	int ret;

	printf("Task Policies:\n");
	ret = bpf_map_get_next_key(fd, NULL, &next_key);
	while (!ret) {
		key = next_key;
		if (!bpf_map_lookup_elem(fd, &key, &policy))
			printf("pid=%u class_id=%u weight=%u class=%s\n", key,
			       policy.class_id, policy.weight,
			       class_name(policy.class_id));
		ret = bpf_map_get_next_key(fd, &key, &next_key);
	}
	printf("Cgroup Policies:\n");
	ret = bpf_map_get_next_key(cgroup_fd, NULL, &next_cgroup_key);
	while (!ret) {
		cgroup_key = next_cgroup_key;
		if (!bpf_map_lookup_elem(cgroup_fd, &cgroup_key, &policy))
			printf("cgroup_id=%llu class_id=%u weight=%u class=%s\n",
			       cgroup_key, policy.class_id, policy.weight,
			       class_name(policy.class_id));
		ret = bpf_map_get_next_key(cgroup_fd, &cgroup_key,
					   &next_cgroup_key);
	}
}

static void dump_cgroup_metrics(struct scx_agent *skel)
{
	int fd = bpf_map__fd(skel->maps.cgroup_metrics_map);
	__u64 key, next_key;
	struct schedx_cgroup_metrics metrics;
	int ret;

	printf("Cgroup Metrics:\n");
	ret = bpf_map_get_next_key(fd, NULL, &next_key);
	while (!ret) {
		key = next_key;
		if (!bpf_map_lookup_elem(fd, &key, &metrics))
			printf("cgroup_id=%llu enqueues=%llu runs=%llu runtime_ns=%llu wait_ns=%llu\n",
			       key, metrics.enqueues, metrics.runs,
			       metrics.runtime_ns, metrics.wait_ns);
		ret = bpf_map_get_next_key(fd, &key, &next_key);
	}
}

static void process_command(struct scx_agent *skel, char *line)
{
	char scope[16];
	__u32 pid, class_id, weight;
	__u32 background_interval, default_interval;
	__u64 cgroup_id;

	if (!strcmp(line, "quit") || !strcmp(line, "exit")) {
		exit_req = 1;
		return;
	}
	if (!strcmp(line, "stats")) {
		print_stats(skel);
		return;
	}
	if (!strcmp(line, "dump") || !strcmp(line, "policies")) {
		dump_policies(skel);
		return;
	}
	if (!strcmp(line, "metrics")) {
		dump_cgroup_metrics(skel);
		return;
	}
	if (!strcmp(line, "class_metrics")) {
		int cpus = libbpf_num_possible_cpus();
		struct schedx_class_metrics values[cpus];
		for (__u32 id = 0; id < SCX_CLASS_COUNT; id++) {
			struct schedx_class_metrics total = {};
			memset(values, 0, sizeof(values));
			if (bpf_map_lookup_elem(bpf_map__fd(skel->maps.class_metrics_map), &id, values))
				continue;
			for (int cpu = 0; cpu < cpus; cpu++) {
				total.enqueues += values[cpu].enqueues;
				total.runs += values[cpu].runs;
				total.runtime_ns += values[cpu].runtime_ns;
				total.wait_ns += values[cpu].wait_ns;
				if (values[cpu].max_wait_ns > total.max_wait_ns)
					total.max_wait_ns = values[cpu].max_wait_ns;
			}
			printf("class_id=%u enqueues=%llu runs=%llu runtime_ns=%llu wait_ns=%llu max_wait_ns=%llu\n",
			       id, total.enqueues, total.runs, total.runtime_ns, total.wait_ns, total.max_wait_ns);
		}
		return;
	}
	if (sscanf(line, "set %15s %u %u %u", scope, &pid, &class_id,
		   &weight) == 4 && !strcmp(scope, "task")) {
		if (set_task_policy(skel, pid, class_id, weight))
			fprintf(stderr, "failed to set task policy: %s\n",
				strerror(errno));
		else
			printf("policy updated pid=%u class=%s weight=%u\n", pid,
			       class_name(class_id), weight);
		return;
	}
	if (sscanf(line, "set %15s %llu %u %u", scope, &cgroup_id,
		   &class_id, &weight) == 4 && !strcmp(scope, "cgroup")) {
		if (set_cgroup_policy(skel, cgroup_id, class_id, weight))
			fprintf(stderr, "failed to set cgroup policy: %s\n",
				strerror(errno));
		else
			printf("policy updated cgroup_id=%llu class=%s weight=%u\n",
			       cgroup_id, class_name(class_id), weight);
		return;
	}
	if (sscanf(line, "set fairness %u %u", &background_interval,
		   &default_interval) == 2) {
		if (set_fairness(skel, background_interval, default_interval))
			fprintf(stderr, "failed to set fairness: %s\n",
				strerror(errno));
		else
			printf("fairness updated background_interval=%u default_interval=%u\n",
			       background_interval, default_interval);
		return;
	}
	if (sscanf(line, "remove %15s %u", scope, &pid) == 2 &&
	    !strcmp(scope, "task")) {
		if (bpf_map_delete_elem(bpf_map__fd(skel->maps.task_policy_map),
				       &pid) && errno != ENOENT)
			fprintf(stderr, "failed to remove task policy: %s\n",
				strerror(errno));
		else
			printf("policy removed pid=%u\n", pid);
		return;
	}
	if (sscanf(line, "remove %15s %llu", scope, &cgroup_id) == 2 &&
	    !strcmp(scope, "cgroup")) {
		if (bpf_map_delete_elem(bpf_map__fd(skel->maps.cgroup_policy_map),
				       &cgroup_id) && errno != ENOENT)
			fprintf(stderr, "failed to remove cgroup policy: %s\n",
				strerror(errno));
		else
			printf("policy removed cgroup_id=%llu\n", cgroup_id);
		bpf_map_delete_elem(bpf_map__fd(skel->maps.cgroup_metrics_map),
				    &cgroup_id);
		return;
	}
	if (sscanf(line, "remove %15s %llu", scope, &cgroup_id) == 2 &&
	    !strcmp(scope, "cgroup-metrics")) {
		if (bpf_map_delete_elem(bpf_map__fd(skel->maps.cgroup_metrics_map),
				       &cgroup_id) && errno != ENOENT)
			fprintf(stderr, "failed to remove cgroup metrics: %s\n",
				strerror(errno));
		else
			printf("metrics removed cgroup_id=%llu\n", cgroup_id);
		return;
	}
	fprintf(stderr, "unknown command: %s\n", line);
}

int main(int argc, char **argv)
{
	struct scx_agent *skel;
	struct bpf_link *link;
	char line[256];
	__u64 ecode;
	bool once = false;
	int opt;

	while ((opt = getopt(argc, argv, "1h")) != -1) {
		if (opt == '1')
			once = true;
		else {
			printf("Usage: %s [-1]\n", argv[0]);
			printf("  -1  attach briefly, print stats, and exit\n");
			return opt != 'h';
		}
	}

	signal(SIGINT, signal_handler);
	signal(SIGTERM, signal_handler);

	skel = SCX_OPS_OPEN(schedx_ops, scx_agent);
	SCX_OPS_LOAD(skel, schedx_ops, scx_agent, uei);
	link = SCX_OPS_ATTACH(skel, schedx_ops, scx_agent);

	if (set_fairness(skel, 64, 32)) {
		fprintf(stderr, "failed to initialize service guarantees\n");
		bpf_link__destroy(link);
		scx_agent__destroy(skel);
		return 1;
	}
	printf("SchedX native sched_ext scheduler attached.\n");
	if (once) {
		sleep(2);
		print_stats(skel);
	} else {
		print_prompt();
		while (!exit_req && !UEI_EXITED(skel, uei) &&
		       fgets(line, sizeof(line), stdin)) {
			line[strcspn(line, "\n")] = '\0';
			process_command(skel, line);
			if (!exit_req)
				print_prompt();
		}
	}

	bpf_link__destroy(link);
	ecode = UEI_REPORT(skel, uei);
	scx_agent__destroy(skel);
	return UEI_ECODE_RESTART(ecode) ? 2 : 0;
}
