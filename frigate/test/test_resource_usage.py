"""Verify resource denominators, raw CPU compatibility and resident-memory units."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frigate.util import resource


class TestResourceLimits(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cgroup = self.root / "cgroups"
        self.cgroup.mkdir()
        self.membership = self.root / "self-cgroup"
        self.membership.write_text("0::/\n")
        for context in (
            patch.object(resource, "CGROUP_ROOT", self.cgroup),
            patch.object(resource, "SELF_CGROUP", self.membership),
            patch.object(resource.psutil, "cpu_count", return_value=64),
            patch.object(
                resource.psutil,
                "virtual_memory",
                return_value=SimpleNamespace(total=32 * 2**30),
            ),
            patch.object(
                resource.os,
                "sched_getaffinity",
                return_value=set(range(64)),
                create=True,
            ),
        ):
            context.start()
            self.addCleanup(context.stop)

    def unified(self, cpu="max 100000", memory="max"):
        (self.cgroup / "cgroup.controllers").write_text("cpu memory")
        (self.cgroup / "cpu.max").write_text(cpu)
        (self.cgroup / "memory.max").write_text(memory)

    def test_unlimited_host_capacity_is_not_one_core(self):
        self.unified()
        result = resource.get_resource_limits()
        self.assertEqual(result["cpu_capacity"], 64)
        self.assertEqual(result["memory_capacity_bytes"], 32 * 2**30)
        self.assertEqual(result["cpu_scope"], "host")
        self.assertEqual(result["errors"], [])

    def test_fractional_quota_and_memory_limit(self):
        self.unified(cpu="250000 100000", memory=str(2**30))
        result = resource.get_resource_limits()
        self.assertEqual(result["cpu_capacity"], 2.5)
        self.assertEqual(result["memory_capacity_bytes"], 2**30)
        self.assertEqual(result["cpu_scope"], "container")
        self.assertEqual(result["memory_scope"], "container")

    def test_affinity_is_an_upper_bound_even_with_larger_quota(self):
        self.unified(cpu="800000 100000")
        with patch.object(resource.os, "sched_getaffinity", return_value={0, 1}):
            self.assertEqual(resource.get_resource_limits()["cpu_capacity"], 2)

    def test_visible_parent_limits_apply_to_unlimited_child(self):
        self.unified()
        parent = self.cgroup / "group"
        child = parent / "child"
        child.mkdir(parents=True)
        (parent / "cpu.max").write_text("150000 100000")
        (parent / "memory.max").write_text(str(512 * 2**20))
        (child / "cpu.max").write_text("max 100000")
        (child / "memory.max").write_text("max")
        self.membership.write_text("0::/group/child\n")
        result = resource.get_resource_limits()
        self.assertEqual(result["cpu_capacity"], 1.5)
        self.assertEqual(result["memory_capacity_bytes"], 512 * 2**20)

    def test_v1_quotas_and_unlimited_memory_sentinel(self):
        cpu = self.cgroup / "cpu"
        memory = self.cgroup / "memory"
        cpu.mkdir()
        memory.mkdir()
        (cpu / "cpu.cfs_quota_us").write_text("75000")
        (cpu / "cpu.cfs_period_us").write_text("100000")
        (memory / "memory.limit_in_bytes").write_text(str(2**63 - 4096))
        self.membership.write_text("2:cpu,cpuacct:/\n3:memory:/\n")
        result = resource.get_resource_limits()
        self.assertEqual(result["cpu_capacity"], 0.75)
        self.assertEqual(result["memory_scope"], "host")

    def test_bad_cpu_limit_is_unknown_not_host_capacity_or_zero(self):
        self.unified(cpu="0 100000")
        with self.assertLogs("frigate.util.resource", level="ERROR"):
            result = resource.get_resource_limits()
        self.assertIsNone(result["cpu_capacity"])
        self.assertEqual(result["cpu_scope"], "unknown")
        self.assertIn("cpu_quota_unavailable", result["errors"])

    def test_bad_memory_limit_does_not_invent_zero_usage(self):
        self.unified(memory="not-a-limit")
        with self.assertLogs("frigate.util.resource", level="ERROR"):
            result = resource.get_resource_limits()
        self.assertIsNone(result["memory_capacity_bytes"])
        self.assertEqual(result["memory_scope"], "unknown")

    def test_limits_larger_than_host_do_not_inflate_capacity(self):
        self.unified(cpu="12800000 100000", memory=str(64 * 2**30))
        result = resource.get_resource_limits()
        self.assertEqual(result["cpu_capacity"], 64)
        self.assertEqual(result["memory_capacity_bytes"], 32 * 2**30)


@unittest.skipIf(sys.platform == "win32", "Process collection uses Linux /proc")
class TestProcessResourceStats(unittest.TestCase):
    def test_raw_cpu_is_preserved_and_resident_memory_is_not_virtual_size(self):
        from frigate.util import services

        fields = ["0"] * 30
        fields[0], fields[11], fields[12], fields[19] = "S", "61", "13", "100"
        files = {
            "/proc/123/stat": "123 (go2rtc worker) " + " ".join(fields),
            "/proc/123/statm": "999999 256 0 0 0 0 0",
            "/proc/uptime": "2.5 0",
        }
        process = SimpleNamespace(
            info={
                "pid": 123,
                "name": "go2rtc",
                "cpu_percent": 53.0,
                "cmdline": ["go2rtc"],
            }
        )

        def opened(path, *_args, **_kwargs):
            return io.StringIO(files[path])

        def sysconf(name):
            return 4096 if name == "SC_PAGE_SIZE" else 100

        with (
            patch.object(
                services,
                "get_resource_limits",
                return_value={"memory_capacity_bytes": 2**30},
            ),
            patch.object(services.psutil, "cpu_percent", return_value=0.0),
            patch.object(
                services.psutil,
                "virtual_memory",
                return_value=SimpleNamespace(percent=0.0),
            ),
            patch.object(services.psutil, "process_iter", return_value=[process]),
            patch.object(services.os, "sysconf", side_effect=sysconf),
            patch("builtins.open", side_effect=opened),
        ):
            stats = services.get_cpu_stats()
        self.assertEqual(stats["123"]["cpu"], "53.0")
        self.assertEqual(stats["123"]["cpu_average"], "49.3333")
        self.assertEqual(stats["123"]["mem_bytes"], 2**20)
        self.assertAlmostEqual(float(stats["123"]["mem"]), 0.09765625)
        self.assertEqual(stats["frigate.full_system"]["cpu"], "0.0")


class TestPrometheusResourceStats(unittest.TestCase):
    def collect(self, capacity=64):
        from frigate.stats.prometheus import CustomCollector

        collector = CustomCollector(None)
        collector.complete_stats = {
            "cameras": {},
            "detectors": {},
            "processes": {},
            "bandwidth_usages": {"42": {"bandwidth": 1}},
            "resources": {"cpu_capacity": capacity, "memory_capacity_bytes": 2**30},
            "cpu_usages": {
                "42": {
                    "cpu": "53",
                    "mem": "1e-06",
                    "mem_bytes": 2**20,
                    "cmdline": "go2rtc",
                },
                "43": {"cpu": "0", "mem": "0", "mem_bytes": 0, "cmdline": "idle"},
                "frigate.full_system": {"cpu": "10", "mem": "30"},
            },
            "service": {
                "storage": {
                    "/tmp/cache": {
                        "free": 1,
                        "used": 2,
                        "total": 3,
                        "mount_type": "tmpfs",
                    }
                },
            },
        }
        return {family.name: family for family in collector.collect()}

    def test_normalized_cpu_and_cores_are_additive_not_a_raw_metric_redefinition(self):
        metrics = self.collect()
        share = metrics["frigate_process_cpu_capacity_percent"].samples
        cores = metrics["frigate_process_cpu_cores"].samples
        self.assertAlmostEqual(share[0].value, 53 / 64)
        self.assertEqual(cores[0].value, 0.53)
        self.assertEqual(share[1].value, 0)
        self.assertEqual(len(share), 2)
        raw = metrics["frigate_cpu_usage_percent"].samples
        self.assertEqual(
            next(sample.value for sample in raw if sample.labels["pid"] == "42"), 53
        )
        self.assertEqual(metrics["frigate_host_cpu_usage_percent"].samples[0].value, 10)

    def test_unknown_capacity_omits_normalized_cpu_but_preserves_core_readings(self):
        metrics = self.collect(None)
        self.assertEqual(metrics["frigate_process_cpu_capacity_percent"].samples, [])
        self.assertEqual(len(metrics["frigate_process_cpu_cores"].samples), 2)

    def test_storage_bytes_and_small_scientific_memory_percent_are_exact(self):
        metrics = self.collect()
        self.assertEqual(metrics["frigate_storage_free_bytes"].samples[0].value, 2**20)
        self.assertEqual(
            metrics["frigate_storage_total_bytes"].samples[0].value, 3 * 2**20
        )
        self.assertEqual(
            metrics["frigate_process_resident_memory_bytes"].samples[0].value, 2**20
        )
        memory = metrics["frigate_mem_usage_percent"].samples
        self.assertEqual(
            next(sample.value for sample in memory if sample.labels["pid"] == "42"),
            1e-6,
        )
        self.assertEqual(
            metrics["frigate_bandwidth_bytes_per_second"].samples[0].value, 1024
        )
