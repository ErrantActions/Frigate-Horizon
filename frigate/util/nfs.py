"""Validated host-managed NFS setup plans without mounting or probing exports."""

import hashlib
import ipaddress
import re
from pathlib import PurePosixPath
from typing import Literal

import psutil
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def is_remote_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject local endpoints, including IPv4 addresses wrapped in IPv6."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
    )


class NfsServer(BaseModel):
    """A server address, never a URL, shell expression, or mount option."""

    model_config = ConfigDict(extra="forbid")
    server: str = Field(min_length=1, max_length=253)

    @field_validator("server")
    @classmethod
    def validate_server(cls, value: str) -> str:
        """Accept unicast IP addresses and ordinary DNS hostnames."""
        value = value.strip().rstrip(".").lower()
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*", value):
                raise ValueError(
                    "Enter a canonical IP address or DNS hostname"
                ) from None
            if not re.fullmatch(
                r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
                value,
            ):
                raise ValueError("Enter a server IP address or DNS hostname") from None
            if value == "localhost" or value.endswith(".localhost"):
                raise ValueError("The NFS server must be remote") from None
            return value
        if not is_remote_address(address) or "%" in value:
            raise ValueError("The NFS server must be a remote unicast address")
        return str(address)


class NfsShare(NfsServer):
    """One explicit Docker-managed NFS destination."""

    role: Literal["archive", "recordings"] = "archive"
    export: str = Field(min_length=1, max_length=1024)
    version: Literal["4.1", "4.2", "3"] = "4.1"
    connections: int = Field(default=8, ge=1, le=16, strict=True)

    @field_validator("export")
    @classmethod
    def validate_export(cls, value: str) -> str:
        """Keep export paths separate from server and mount-option syntax."""
        if (
            value == "/"
            or not value.startswith("/")
            or value.startswith("//")
            or not re.fullmatch(r"/[A-Za-z0-9_./-]*", value)
            or any(part in (".", "..") for part in value.split("/"))
        ):
            raise ValueError(
                "Enter an absolute export path without traversal or options"
            )
        return str(PurePosixPath(value))


class NfsPlan(BaseModel):
    """A setup draft, not permission to change mounts or enable recording."""

    model_config = ConfigDict(extra="forbid")
    shares: list[NfsShare] = Field(min_length=1, max_length=2)
    confirm_direct_recordings: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_shares(self) -> "NfsPlan":
        """Reject conflicting roles, destinations, and shared-server settings."""
        if len({share.role for share in self.shares}) != len(self.shares):
            raise ValueError("Only one destination is allowed for each role")
        if len({(share.server, share.export) for share in self.shares}) != len(
            self.shares
        ):
            raise ValueError("Recording and archive destinations must be different")
        for share in self.shares:
            if share.role == "recordings" and not self.confirm_direct_recordings:
                raise ValueError(
                    "Direct NFS recordings require explicit acknowledgment"
                )
        if len(self.shares) == 2:
            first, second = self.shares
            if first.server == second.server and (
                first.connections != second.connections
                or first.version != second.version
            ):
                raise ValueError("Shares on one server must use matching NFS settings")
        return self


def build_nfs_plan(
    plan: NfsPlan, archive_path: str, mount_id: str, local_budget_gib: float
) -> dict:
    """Generate additions only, preserving existing App mounts, GPUs, and identity."""
    volumes = {}
    mounts = []
    for share in plan.shares:
        identity = hashlib.sha256(
            f"{share.server}|{share.export}|{share.version}|{share.connections}".encode()
        ).hexdigest()[:10]
        name = f"frigate_nfs_{share.role}_{identity}"
        volumes[name] = {
            "driver": "local",
            "driver_opts": {
                "type": "nfs",
                "o": (
                    f"addr={share.server},nfsvers={share.version},proto=tcp,"
                    f"hard,rw,nconnect={share.connections}"
                ),
                "device": f":{share.export}",
            },
        }
        mounts.append(
            {
                "type": "volume",
                "source": name,
                "target": archive_path
                if share.role == "archive"
                else "/media/frigate/recordings",
                "volume": {"nocopy": True},
            }
        )
    local_first = not any(share.role == "recordings" for share in plan.shares)
    storage = {
        "enabled": False,
        "archive_path": archive_path,
        "mount_id": mount_id,
        "local_budget_gib": local_budget_gib,
        "parallel_transfers": min(16, max(share.connections for share in plan.shares)),
        "adaptive_transfers": True,
        "emergency_delete": False,
    }
    return {
        "volume_definitions_yaml": yaml.safe_dump(
            {"volumes": volumes}, sort_keys=False
        ),
        "service_volumes_yaml": yaml.safe_dump({"volumes": mounts}, sort_keys=False),
        "frigate_storage_yaml": (
            yaml.safe_dump({"storage": storage}, sort_keys=False)
            if any(share.role == "archive" for share in plan.shares)
            else None
        ),
        "local_first": local_first,
        "requires_app_redeployment": True,
        "enables_recording": False,
        "archive_marker": (
            {"filename": ".frigate-archive", "contents": mount_id}
            if any(share.role == "archive" for share in plan.shares)
            else None
        ),
    }


def nfs_mount_status(target: str, partitions=None) -> dict:
    """Inspect local mount metadata only; never stat a possibly stalled NFS path."""
    partitions = psutil.disk_partitions(all=True) if partitions is None else partitions
    path = PurePosixPath(target)
    covering = [
        (len(PurePosixPath(item.mountpoint).parts), index, item)
        for index, item in enumerate(partitions)
        if path == PurePosixPath(item.mountpoint)
        or PurePosixPath(item.mountpoint) in path.parents
    ]
    mount = max(covering, key=lambda item: item[:2])[2] if covering else None
    mounted = mount is not None and mount.fstype in ("nfs", "nfs4")
    options = (
        dict(
            option.split("=", 1) if "=" in option else (option, "")
            for option in mount.opts.split(",")
        )
        if mounted
        else {}
    )
    connections = options.get("nconnect")
    return {
        "container_path": target,
        "mounted": mounted,
        "filesystem": mount.fstype if mount is not None else None,
        "source": mount.device if mounted else None,
        "mountpoint": mount.mountpoint if mounted else None,
        "configured_connections": (
            int(connections) if connections and connections.isdecimal() else None
        ),
        "version": options.get("vers") or options.get("nfsvers"),
        "access_verified": False,
    }
