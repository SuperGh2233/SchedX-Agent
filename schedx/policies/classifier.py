from __future__ import annotations

LATENCY_NAMES = {
    "nginx",
    "redis-server",
    "redis",
    "envoy",
    "haproxy",
    "mysql",
    "postgres",
    "mongod",
}
BATCH_NAMES = {
    "make",
    "cmake",
    "gcc",
    "g++",
    "cc1",
    "sysbench",
    "cargo",
    "rustc",
    "java",
    "javac",
}
NOISE_NAMES = {
    "stress-ng",
    "stress-ng-cpu",
    "stress",
    "yes",
    "openssl",
    "dd",
    "sha256sum",
}
PYTHON_BATCH_KEYWORDS = ("benchmark", "train", "stress", "load", "compute", "matrix")


class WorkloadClassifier:
    def classify_process(self, proc: dict) -> str:
        return self.classify_process_with_reason(proc)["type"]

    def classify_process_with_reason(self, proc: dict) -> dict[str, str]:
        comm = str(proc.get("comm", "")).lower()
        cmdline = str(proc.get("cmdline", "")).lower()
        text = f"{comm} {cmdline}"
        argv0 = cmdline.split(maxsplit=1)[0].rsplit("/", 1)[-1] if cmdline else ""
        cpu = float(proc.get("cpu_percent", 0.0) or 0.0)
        rss = int(proc.get("rss_bytes", 0) or 0)
        voluntary_ctx = int(proc.get("voluntary_ctxt_switches", 0) or 0)
        sched = proc.get("sched") or {}
        iowait = float(sched.get("se.statistics.iowait_sum", 0.0) or 0.0)
        wait_sum = float(sched.get("se.statistics.wait_sum", 0.0) or 0.0)
        wait_count = float(sched.get("se.statistics.wait_count", 0.0) or 0.0)

        # Python benchmark drivers may mention service names as arguments. Handle
        # them before exact executable matching so the controller cannot become
        # the protected workload by accident.
        if comm in {"python", "python3"}:
            if any(keyword in cmdline for keyword in PYTHON_BATCH_KEYWORDS):
                return {
                    "type": "batch_compute",
                    "reason": f"python cmdline contains batch keyword: {cmdline}",
                }
            return {
                "type": "unknown",
                "reason": "python process without benchmark/train/stress/load keyword",
            }

        # Rule 1: Known latency-sensitive service executable
        if comm in LATENCY_NAMES or argv0 in LATENCY_NAMES:
            return {
                "type": "latency_sensitive",
                "reason": f"comm/cmdline={text.strip()} matches latency service rule",
            }

        # Rule 2: Known background-noise executable
        if comm in NOISE_NAMES or argv0 in NOISE_NAMES:
            return {
                "type": "background_noise",
                "reason": f"comm/cmdline={text.strip()} matches background noise rule",
            }

        # Rule 3: Known batch-compute executable
        if comm in BATCH_NAMES or argv0 in BATCH_NAMES:
            return {
                "type": "batch_compute",
                "reason": f"comm/cmdline={text.strip()} matches batch compute rule",
            }

        # Rule 5: High CPU usage -> batch compute
        if cpu >= 50.0:
            return {"type": "batch_compute", "reason": f"cpu_percent={cpu:.1f} >= 50"}

        # Rule 6: High I/O wait -> I/O bound (treat as latency-sensitive for scheduling)
        if iowait > 100.0 and cpu >= 5.0:
            return {
                "type": "latency_sensitive",
                "reason": f"iowait={iowait:.0f} is high, I/O-bound process needs responsive scheduling",
            }

        # Rule 7: High voluntary context switches -> I/O-bound
        if voluntary_ctx > 10000 and cpu >= 5.0:
            return {
                "type": "latency_sensitive",
                "reason": f"voluntary_ctxt_switches={voluntary_ctx} > 10000, I/O-bound process",
            }

        # Rule 8: High scheduling wait + CPU usage -> batch compute
        if wait_count > 0 and wait_sum / wait_count > 5.0 and cpu >= 10.0:
            return {
                "type": "batch_compute",
                "reason": f"sched_wait_avg={wait_sum/wait_count:.1f}ms > 5ms, cpu={cpu:.1f}%",
            }

        # Rule 9: Large memory footprint with some CPU -> likely important service
        if rss > 500 * 1024 * 1024 and cpu >= 2.0:
            return {
                "type": "latency_sensitive",
                "reason": f"rss={rss/(1024*1024):.0f}MB > 500MB, cpu={cpu:.1f}%, likely important service",
            }

        return {"type": "unknown", "reason": "no workload rule matched"}

    def classify_snapshot(self, snapshot: dict) -> dict:
        groups: dict[str, list[dict]] = {
            "latency_sensitive": [],
            "batch_compute": [],
            "background_noise": [],
            "unknown": [],
        }
        for proc in snapshot.get("processes", []):
            decision = self.classify_process_with_reason(proc)
            annotated = dict(proc)
            annotated["workload_type"] = decision["type"]
            annotated["reason"] = decision["reason"]
            groups[decision["type"]].append(annotated)

        for items in groups.values():
            items.sort(key=self._sort_key, reverse=True)

        active_types = [
            key for key, items in groups.items() if key != "unknown" and items
        ]
        overall = (
            "mixed"
            if len(active_types) > 1
            else (active_types[0] if active_types else "unknown")
        )

        # Add system-level PSI analysis
        pressure = snapshot.get("pressure", {})
        psi_insight = self._analyze_psi(pressure)

        return {"overall": overall, "groups": groups, "psi_insight": psi_insight}

    def _analyze_psi(self, pressure: dict) -> dict[str, str]:
        """Analyze system-level PSI data for workload insights."""
        insights: dict[str, str] = {}
        cpu_psi = pressure.get("cpu", {}).get("some", {})
        mem_psi = pressure.get("memory", {}).get("some", {})
        io_psi = pressure.get("io", {}).get("some", {})

        if cpu_psi:
            avg10 = cpu_psi.get("avg10", 0.0)
            if avg10 > 20.0:
                insights["cpu"] = (
                    f"CPU pressure high (avg10={avg10:.1f}%), system is CPU-bound"
                )
            elif avg10 > 5.0:
                insights["cpu"] = f"CPU pressure moderate (avg10={avg10:.1f}%)"

        if mem_psi:
            avg10 = mem_psi.get("avg10", 0.0)
            if avg10 > 10.0:
                insights["memory"] = (
                    f"Memory pressure high (avg10={avg10:.1f}%), consider memory limits"
                )

        if io_psi:
            avg10 = io_psi.get("avg10", 0.0)
            if avg10 > 20.0:
                insights["io"] = (
                    f"I/O pressure high (avg10={avg10:.1f}%), system is I/O-bound"
                )

        return insights

    def _sort_key(self, proc: dict) -> tuple[int, float, int]:
        matched = 0 if proc.get("workload_type") == "unknown" else 1
        return (
            matched,
            float(proc.get("cpu_percent", 0.0) or 0.0),
            int(proc.get("rss_bytes", 0) or 0),
        )
