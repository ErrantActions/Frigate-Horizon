"""Bounded JSON-lines RPC to a trusted, independently stoppable Python process."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from frigate.plugins.models import WorkerReply
from frigate.plugins.packages import PluginError


class PluginWorker:
    """Contain hangs and crashes, not malicious code inside the container."""

    def __init__(self, entrypoint: Path, data: Path) -> None:
        self.entrypoint = entrypoint
        self.data = data
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.sequence = 0

    async def start(self) -> None:
        """Start with a minimal environment and no inherited credentials."""
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
        }
        environment["HORIZON_PLUGIN_DATA"] = str(self.data.resolve())
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            str(self.entrypoint.resolve()),
            cwd=self.entrypoint.parent,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=1024 * 1024,
        )

    async def call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Serialize requests and terminate a worker that violates its deadline."""
        async with self.lock:
            process = self.process
            if (
                process is None
                or process.returncode is not None
                or process.stdin is None
                or process.stdout is None
            ):
                raise PluginError("worker_not_running")
            self.sequence += 1
            message = {"id": self.sequence, "method": method, "params": params or {}}
            try:
                async with asyncio.timeout(25):
                    process.stdin.write((json.dumps(message) + "\n").encode())
                    await process.stdin.drain()
                    line = await process.stdout.readline()
                    reply = WorkerReply.model_validate_json(line)
                    if reply.id != self.sequence or reply.error is not None:
                        raise PluginError("worker_request_failed")
                    if reply.result is None:
                        raise PluginError("worker_invalid_response")
                    return reply.result
            except (TimeoutError, ValueError, BrokenPipeError, ConnectionResetError):
                await self.close()
                raise PluginError("worker_protocol_failed") from None

    async def close(self) -> None:
        """Reap only this worker, escalating to kill after a bounded wait."""
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
