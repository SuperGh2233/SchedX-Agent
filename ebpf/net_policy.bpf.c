/* SPDX-License-Identifier: GPL-2.0 */
/*
 * net_policy.bpf.c - Network policy enforcement using eBPF
 *
 * This BPF program implements network traffic control using TC (Traffic Control)
 * hooks. It can classify and shape network traffic based on workload type.
 *
 * Features:
 * - Per-task network bandwidth limiting
 * - Traffic classification based on workload type
 * - Rate limiting for background/noise processes
 * - Priority queuing for latency-sensitive tasks
 */

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>

/* Network policy constants */
#define NET_CLASS_LATENCY     1
#define NET_CLASS_BATCH       2
#define NET_CLASS_BACKGROUND  3
#define NET_CLASS_UNKNOWN     0

/* Rate limits in bytes per second */
#define RATE_LIMIT_LATENCY    1000000000  /* 1 GB/s - essentially unlimited */
#define RATE_LIMIT_BATCH      100000000   /* 100 MB/s */
#define RATE_LIMIT_BACKGROUND 10000000    /* 10 MB/s */
#define RATE_LIMIT_DEFAULT    500000000   /* 500 MB/s */

/* Token bucket parameters */
#define BURST_SIZE_LATENCY    10000000    /* 10 MB burst */
#define BURST_SIZE_BATCH      1000000     /* 1 MB burst */
#define BURST_SIZE_BACKGROUND 100000      /* 100 KB burst */

/* Network policy entry */
struct net_policy {
    __u32 class_id;         /* NET_CLASS_* */
    __u32 rate_limit_bps;   /* Rate limit in bytes/sec */
    __u32 burst_size;       /* Token bucket burst size */
    __u32 priority;         /* Queue priority (0-7) */
};

/* Token bucket state */
struct token_bucket {
    __u64 tokens;           /* Available tokens (bytes) */
    __u64 last_refill;      /* Last refill timestamp */
    __u32 rate_bps;         /* Refill rate in bytes/sec */
    __u32 burst_size;       /* Maximum burst size */
};

/* Network statistics per task */
struct net_stats {
    __u64 bytes_sent;
    __u64 bytes_received;
    __u64 packets_sent;
    __u64 packets_received;
    __u64 packets_dropped;
    __u64 bytes_dropped;
};

/* BPF Maps */

/* Per-task network policy: PID -> policy */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u32);              /* PID */
    __type(value, struct net_policy);
} net_policy_map SEC(".maps");

/* Per-task token bucket: PID -> bucket state */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u32);              /* PID */
    __type(value, struct token_bucket);
} token_bucket_map SEC(".maps");

/* Per-task network statistics (per-CPU) */
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __uint(max_entries, 4096);
    __type(key, __u32);              /* PID */
    __type(value, struct net_stats);
} net_stats_map SEC(".maps");

/* Global network statistics (per-CPU) */
struct global_net_stats {
    __u64 total_packets;
    __u64 total_bytes;
    __u64 dropped_packets;
    __u64 dropped_bytes;
};

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct global_net_stats);
} global_net_stats_map SEC(".maps");

/* Helper: Refill token bucket */
static __always_inline bool refill_tokens(struct token_bucket *bucket, __u64 now)
{
    __u64 elapsed = now - bucket->last_refill;
    __u64 new_tokens = (elapsed * bucket->rate_bps) / 1000000000ULL;

    bucket->tokens += new_tokens;
    if (bucket->tokens > bucket->burst_size)
        bucket->tokens = bucket->burst_size;

    bucket->last_refill = now;
    return true;
}

/* Helper: Try to consume tokens */
static __always_inline bool consume_tokens(struct token_bucket *bucket, __u32 bytes)
{
    if (bucket->tokens >= bytes) {
        bucket->tokens -= bytes;
        return true;
    }
    return false;
}

/* Helper: Update statistics */
static __always_inline void update_net_stats(__u32 pid, __u32 bytes, bool is_send, bool dropped)
{
    struct net_stats *stats;
    stats = bpf_map_lookup_elem(&net_stats_map, &pid);
    if (!stats) {
        struct net_stats new_stats = {};
        bpf_map_update_elem(&net_stats_map, &pid, &new_stats, BPF_ANY);
        stats = bpf_map_lookup_elem(&net_stats_map, &pid);
        if (!stats)
            return;
    }

    if (dropped) {
        stats->packets_dropped++;
        stats->bytes_dropped += bytes;
    } else if (is_send) {
        stats->packets_sent++;
        stats->bytes_sent += bytes;
    } else {
        stats->packets_received++;
        stats->bytes_received += bytes;
    }
}

/* Helper: Update global statistics */
static __always_inline void update_global_stats(__u32 bytes, bool dropped)
{
    __u32 key = 0;
    struct global_net_stats *stats;

    stats = bpf_map_lookup_elem(&global_net_stats_map, &key);
    if (!stats)
        return;

    stats->total_packets++;
    stats->total_bytes += bytes;

    if (dropped) {
        stats->dropped_packets++;
        stats->dropped_bytes += bytes;
    }
}

/*
 * TC classifier for egress traffic
 *
 * This program is attached to the TC egress hook and enforces
 * network policies for outgoing traffic.
 */
SEC("tc")
int net_policy_egress(struct __sk_buff *skb)
{
    __u32 pid = bpf_get_current_pid_tgid() >> 32;
    __u64 now = bpf_ktime_get_ns();

    /* Get network policy for this task */
    struct net_policy *policy;
    policy = bpf_map_lookup_elem(&net_policy_map, &pid);
    if (!policy) {
        /* No policy, allow traffic */
        return TC_ACT_OK;
    }

    /* Get packet size */
    __u32 pkt_len = skb->len;

    /* Get or create token bucket */
    struct token_bucket *bucket;
    bucket = bpf_map_lookup_elem(&token_bucket_map, &pid);
    if (!bucket) {
        struct token_bucket new_bucket = {
            .tokens = policy->burst_size,
            .last_refill = now,
            .rate_bps = policy->rate_limit_bps,
            .burst_size = policy->burst_size,
        };
        bpf_map_update_elem(&token_bucket_map, &pid, &new_bucket, BPF_ANY);
        bucket = bpf_map_lookup_elem(&token_bucket_map, &pid);
        if (!bucket) {
            update_net_stats(pid, pkt_len, true, false);
            update_global_stats(pkt_len, false);
            return TC_ACT_OK;
        }
    }

    /* Refill tokens */
    refill_tokens(bucket, now);

    /* Check if we can send */
    if (consume_tokens(bucket, pkt_len)) {
        /* Allow packet */
        update_net_stats(pid, pkt_len, true, false);
        update_global_stats(pkt_len, false);
        return TC_ACT_OK;
    }

    /* Rate limited - drop packet */
    update_net_stats(pid, pkt_len, true, true);
    update_global_stats(pkt_len, true);

    /* For latency-sensitive tasks, we might want to queue instead of drop */
    if (policy->class_id == NET_CLASS_LATENCY) {
        /* Use higher queue priority */
        skb->priority = 7;  /* Highest priority */
        return TC_ACT_OK;
    }

    return TC_ACT_SHOT;  /* Drop packet */
}

/*
 * TC classifier for ingress traffic
 *
 * This program is attached to the TC ingress hook and can be used
 * for traffic classification and marking.
 */
SEC("tc")
int net_policy_ingress(struct __sk_buff *skb)
{
    __u32 pid = bpf_get_current_pid_tgid() >> 32;

    /* Get packet size */
    __u32 pkt_len = skb->len;

    /* Update receive statistics */
    update_net_stats(pid, pkt_len, false, false);
    update_global_stats(pkt_len, false);

    /* For now, allow all ingress traffic */
    return TC_ACT_OK;
}

/*
 * XDP program for fast-path packet filtering
 *
 * This can be used for very early packet filtering before the kernel
 * network stack processes the packet.
 */
SEC("xdp")
int net_policy_xdp(struct xdp_md *ctx)
{
    /* For now, pass all packets through */
    return XDP_PASS;
}

char LICENSE[] SEC("license") = "GPL";
