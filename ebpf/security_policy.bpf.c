/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Explicit, default-allow execution policy using BPF LSM.
 *
 * A policy is scoped to one cgroup v2 ID. Audit-only is the safe default;
 * execution is denied only when deny_exec is explicitly set by userspace.
 */

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

struct security_key {
    __u64 cgroup_id;
    __u64 device;
    __u64 inode;
};

struct security_policy {
    __u32 deny_exec;
    __u32 audit_only;
};

struct security_stats {
    __u64 exec_checks;
    __u64 exec_denied;
};

struct exec_identity {
    __u64 device;
    __u64 inode;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, struct security_key);
    __type(value, struct security_policy);
} sec_policy_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct security_stats);
} sec_stats_map SEC(".maps");

/* Last executable identity observed per cgroup, useful for audit/debugging. */
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, 1024);
    __type(key, __u64);
    __type(value, struct exec_identity);
} sec_exec_map SEC(".maps");

SEC("lsm/bprm_check_security")
int BPF_PROG(check_exec, struct linux_binprm *bprm, int ret)
{
    if (ret)
        return ret;

    struct file *file = BPF_CORE_READ(bprm, file);
    struct inode *inode = file ? BPF_CORE_READ(file, f_inode) : 0;
    struct super_block *super = inode ? BPF_CORE_READ(inode, i_sb) : 0;
    if (!inode || !super)
        return 0;

    __u64 cgroup_id = bpf_get_current_cgroup_id();
    struct security_key policy_key = {
        .cgroup_id = cgroup_id,
        .device = BPF_CORE_READ(super, s_dev),
        .inode = BPF_CORE_READ(inode, i_ino),
    };
    struct exec_identity identity = {
        .device = policy_key.device,
        .inode = policy_key.inode,
    };
    bpf_map_update_elem(&sec_exec_map, &cgroup_id, &identity, BPF_ANY);

    __u32 key = 0;
    struct security_stats *stats = bpf_map_lookup_elem(&sec_stats_map, &key);
    if (stats)
        stats->exec_checks++;

    struct security_policy *policy = bpf_map_lookup_elem(&sec_policy_map, &policy_key);
    if (!policy)
        return 0;

    if (policy->deny_exec && !policy->audit_only) {
        if (stats)
            stats->exec_denied++;
        return -13; /* -EACCES */
    }
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
