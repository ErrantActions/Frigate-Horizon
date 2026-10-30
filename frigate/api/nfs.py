"""Administrator-only NFS setup assistance without host mount privileges."""

import asyncio
import ipaddress
import logging
import os
import socket

from fastapi import APIRouter, Depends, HTTPException, Request

from frigate.api.auth import require_role
from frigate.api.defs.tags import Tags
from frigate.util.nfs import (
    NfsPlan,
    NfsServer,
    build_nfs_plan,
    is_remote_address,
    nfs_mount_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/storage/nfs",
    tags=[Tags.recordings],
    dependencies=[Depends(require_role(["admin"]))],
)


@router.get("")
async def get_nfs_status(request: Request):
    """Report mount metadata without performing remote filesystem operations."""
    settings = request.app.frigate_config.storage
    try:
        mounts = await asyncio.to_thread(
            lambda: {
                "archive": nfs_mount_status(settings.archive_path),
                "recordings": nfs_mount_status("/media/frigate/recordings"),
            }
        )
    except OSError:
        logger.exception("Unable to read NFS mount metadata")
        raise HTTPException(503, "Unable to read NFS mount metadata") from None
    return {
        "mounts": mounts,
        "archive_path": settings.archive_path,
        "mount_id": settings.mount_id,
        "local_budget_gib": settings.local_budget_gib,
        "runtime_uid": os.geteuid() if os.name == "posix" else None,
        "runtime_gid": os.getegid() if os.name == "posix" else None,
        "automatic_remounts": False,
        "recording_enabled": request.app.frigate_config.record.enabled,
    }


@router.post("/plan")
async def generate_nfs_plan(request: Request, body: NfsPlan):
    """Generate mergeable App additions without saving settings or mounting anything."""
    settings = request.app.frigate_config.storage
    return build_nfs_plan(
        body, settings.archive_path, settings.mount_id, settings.local_budget_gib
    )


@router.post("/probe")
async def probe_nfs_server(body: NfsServer):
    """Test only TCP port 2049, not export permissions, filesystem health, or speed."""
    error = "unreachable"
    try:
        async with asyncio.timeout(3):
            addresses = await asyncio.get_running_loop().getaddrinfo(
                body.server, 2049, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
            for family, _type, _proto, _name, address in addresses[:8]:
                ip = ipaddress.ip_address(address[0])
                if not is_remote_address(ip):
                    error = "address_not_remote"
                    continue
                try:
                    _reader, writer = await asyncio.open_connection(
                        str(ip), 2049, family=family
                    )
                except OSError:
                    continue
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    logger.debug("NFS probe peer closed during cleanup")
                return {"reachable": True, "port": 2049, "export_verified": False}
    except TimeoutError:
        error = "timeout"
    except socket.gaierror:
        error = "dns_failed"
    logger.warning("NFS TCP reachability check failed: %s", error)
    return {
        "reachable": False,
        "port": 2049,
        "export_verified": False,
        "error": error,
    }
