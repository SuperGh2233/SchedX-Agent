/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Network policy for cgroup v2 workloads.
 *
 * Network packets are not reliably attributable to the current userspace PID
 * at a TC hook. A cgroup_skb hook carries the workload identity through the
 * socket, so policies are keyed by the cgroup v2 ID instead.
 */

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>

#define NET_CLASS_LATENCY 1

struct net_policy {
    __u32 class_id;
    __u32 rate_limit_bps;
    __u32 burst_size;
    __u32 priority;
};

struct schedx_token_bucket {
    struct bpf_spin_lock lock;
    __u32 reserved;
    __u64 tokens;
    __u64 last_refill;
    __u32 rate_bps;
    __u32 burst_size;
};

struct net_stats {
    __u64 bytes_sent;
    __u64 packets_sent;
    __u64 packets_dropped;
    __u64 bytes_dropped;
};

struct global_net_stats {
    __u64 total_packets;
    __u64 total_bytes;
    __u64 dropped_packets;
    __u64 dropped_bytes;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, struct net_policy);
} net_policy_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, struct schedx_token_bucket);
} net_bucket_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, struct net_stats);
} net_stats_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct global_net_stats);
} net_global_map SEC(".maps");

static __always_inline void account_packet(__u64 cgroup_id, __u32 bytes, bool dropped)
{
    struct net_stats *stats = bpf_map_lookup_elem(&net_stats_map, &cgroup_id);
    if (!stats) {
        struct net_stats initial = {};
        bpf_map_update_elem(&net_stats_map, &cgroup_id, &initial, BPF_NOEXIST);
        stats = bpf_map_lookup_elem(&net_stats_map, &cgroup_id);
    }

    if (stats) {
        if (dropped) {
            stats->packets_dropped++;
            stats->bytes_dropped += bytes;
        } else {
            stats->packets_sent++;
            stats->bytes_sent += bytes;
        }
    }

    __u32 key = 0;
    struct global_net_stats *global = bpf_map_lookup_elem(&net_global_map, &key);
    if (global) {
        global->total_packets++;
        global->total_bytes += bytes;
        if (dropped) {
            global->dropped_packets++;
            global->dropped_bytes += bytes;
        }
    }
}

SEC("cgroup_skb/egress")
int net_policy_egress(struct __sk_buff *skb)
{
    __u64 cgroup_id = bpf_skb_cgroup_id(skb);
    struct net_policy *policy = bpf_map_lookup_elem(&net_policy_map, &cgroup_id);
    if (!policy)
        return 1;

    __u64 now = bpf_ktime_get_ns();
    struct schedx_token_bucket *bucket = bpf_map_lookup_elem(&net_bucket_map, &cgroup_id);
    if (!bucket) {
        struct schedx_token_bucket initial = {
            .tokens = policy->burst_size,
            .last_refill = now,
            .rate_bps = policy->rate_limit_bps,
            .burst_size = policy->burst_size,
        };
        bpf_map_update_elem(&net_bucket_map, &cgroup_id, &initial, BPF_NOEXIST);
        bucket = bpf_map_lookup_elem(&net_bucket_map, &cgroup_id);
    }

    if (!bucket) {
        account_packet(cgroup_id, skb->len, false);
        return 1;
    }

    bool allowed = false;
    bpf_spin_lock(&bucket->lock);
    __u64 elapsed = now - bucket->last_refill;
    __u64 refill = elapsed >= 1000000000ULL
                     ? bucket->burst_size
                     : elapsed * bucket->rate_bps / 1000000000ULL;
    __u64 tokens = bucket->tokens + refill;
    if (tokens > bucket->burst_size)
        tokens = bucket->burst_size;
    bucket->last_refill = now;

    if (tokens >= skb->len) {
        bucket->tokens = tokens - skb->len;
        allowed = true;
    } else {
        bucket->tokens = tokens;
    }
    bpf_spin_unlock(&bucket->lock);

    if (allowed) {
        account_packet(cgroup_id, skb->len, false);
        return 1;
    }
    account_packet(cgroup_id, skb->len, true);

    /* Latency-sensitive workloads are measured but never dropped. */
    if (policy->class_id == NET_CLASS_LATENCY)
        return 1;
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
