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

static void dump_policies(struct scx_agent *skel)
{
	int fd = bpf_map__fd(skel->maps.task_policy_map);
	__u32 key, next_key;
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
	printf("Cgroup Policies: unsupported\n");
}

static void process_command(struct scx_agent *skel, char *line)
{
	char scope[16];
	__u32 pid, class_id, weight;

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
