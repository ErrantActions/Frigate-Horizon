"""Visible CPU and memory capacity for consistently scoped resource percentages."""

import logging
import os
from pathlib import Path
from typing import Literal, TypedDict

import psutil

logger = logging.getLogger(__name__)
CGROUP_ROOT = Path("/sys/fs/cgroup")
SELF_CGROUP = Path("/proc/self/cgroup")


class ResourceLimits(TypedDict):
    """Denominators, not utilization; one CPU core is one logical processor."""

    host_logical_cpus: int
    cpu_affinity: int
    cpu_quota: float | None
    cpu_capacity: float | None
    cpu_scope: Literal["host", "container", "unknown"]
    host_memory_bytes: int
    memory_limit_bytes: int | None
    memory_capacity_bytes: int | None
    memory_scope: Literal["host", "container", "unknown"]
    errors: list[str]


def _directories(controller: str) -> list[Path]:
    roots = (
        [CGROUP_ROOT]
        if (CGROUP_ROOT / "cgroup.controllers").exists()
        else [CGROUP_ROOT / controller, CGROUP_ROOT / "cpu,cpuacct"]
        if controller == "cpu"
        else [CGROUP_ROOT / controller]
    )
    try:
        entries = [
            line.split(":", 2) for line in SELF_CGROUP.read_text().splitlines() if line
        ]
        if any(len(entry) != 3 for entry in entries):
            raise ValueError("Invalid cgroup membership")
    except FileNotFoundError:
        entries = []
    result: list[Path] = []
    for root in roots:
        for _hierarchy, controllers, group in entries:
            if controllers and controller not in controllers.split(","):
                continue
            child = root / group.lstrip("/")
            if (
                child.is_relative_to(root)
                and ".." not in child.parts
                and child.is_dir()
            ):
                result.extend(
                    [child, *child.parents][: len(child.parts) - len(root.parts)]
                )
        result.append(root)
    return list(dict.fromkeys(result))


def get_resource_limits() -> ResourceLimits:
    """Honor affinity and visible cgroup quotas, including fractional CPU limits.

    Unreadable or malformed limits produce an unknown denominator, never a
    success-shaped assumption that the container can use the entire host.
    """
    errors: list[str] = []
    cpu_known = memory_known = True
    host_cpus = psutil.cpu_count() or 0
    if host_cpus <= 0:
        logger.warning("Host logical CPU count is unavailable")
        cpu_known = False
        errors.append("host_cpu_count_unavailable")
    try:
        affinity = (
            len(os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else host_cpus
        )
    except (OSError, NotImplementedError):
        logger.exception("CPU affinity is unavailable")
        affinity = 0
        cpu_known = False
        errors.append("cpu_affinity_unavailable")
    if affinity <= 0:
        cpu_known = False
        errors.append("cpu_affinity_unavailable")
    try:
        host_memory = psutil.virtual_memory().total
    except (OSError, RuntimeError, ValueError):
        logger.exception("Host memory capacity is unavailable")
        host_memory = 0
    if host_memory <= 0:
        logger.warning("Host memory capacity is unavailable")
        memory_known = False
        errors.append("host_memory_capacity_unavailable")
    quotas: list[float] = []
    memories: list[int] = []
    try:
        cpu_dirs = _directories("cpu")
        memory_dirs = _directories("memory")
    except (OSError, ValueError, OverflowError):
        logger.exception("Unable to read resource cgroup membership")
        cpu_dirs = memory_dirs = []
        cpu_known = memory_known = False
        errors.append("cgroup_membership_unavailable")
    for directory in cpu_dirs:
        quota_path = directory / "cpu.max"
        try:
            if quota_path.exists():
                quota, period = quota_path.read_text().split()
            else:
                quota_path = directory / "cpu.cfs_quota_us"
                if not quota_path.exists():
                    continue
                quota = quota_path.read_text().strip()
                period = (directory / "cpu.cfs_period_us").read_text().strip()
            period_value = int(period)
            if period_value <= 0:
                raise ValueError("Invalid CPU quota period")
            if quota in ("max", "-1"):
                continue
            value = int(quota) / period_value
            if value <= 0:
                raise ValueError("Invalid CPU quota")
            quotas.append(value)
        except (OSError, ValueError):
            logger.exception("Unable to determine CPU quota from %s", quota_path)
            cpu_known = False
            errors.append("cpu_quota_unavailable")
    for directory in memory_dirs:
        path = directory / "memory.max"
        try:
            if not path.exists():
                path = directory / "memory.limit_in_bytes"
                if not path.exists():
                    continue
            raw = path.read_text().strip()
            if raw == "max":
                continue
            value = int(raw)
            if value <= 0:
                raise ValueError("Invalid memory limit")
            if value < host_memory:
                memories.append(value)
        except (OSError, ValueError):
            logger.exception("Unable to determine memory limit from %s", path)
            memory_known = False
            errors.append("memory_limit_unavailable")
    quota = min(quotas) if quotas else None
    memory_limit = min(memories) if memories else None
    capacity = min(host_cpus, affinity, quota if quota is not None else host_cpus)
    return {
        "host_logical_cpus": host_cpus,
        "cpu_affinity": affinity,
        "cpu_quota": quota,
        "cpu_capacity": capacity if cpu_known else None,
        "cpu_scope": "unknown"
        if not cpu_known
        else "container"
        if capacity < host_cpus
        else "host",
        "host_memory_bytes": host_memory,
        "memory_limit_bytes": memory_limit,
        "memory_capacity_bytes": (memory_limit or host_memory)
        if memory_known
        else None,
        "memory_scope": "unknown"
        if not memory_known
        else "container"
        if memory_limit is not None
        else "host",
        "errors": list(dict.fromkeys(errors)),
    }
